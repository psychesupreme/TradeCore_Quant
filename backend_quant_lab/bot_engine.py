import os
import logging
import json
import pandas as pd
from datetime import datetime, timedelta
import time
import MetaTrader5 as mt5 
from mt5_interface import MT5Gateway
from analyst import analyze_market_structure, AnalysisRequest, calculate_atr, detect_candlestick_pattern
from models import Candle
from telegram_client import TelegramNotifier
from db_manager import DBManager
from quant_analyzer import QuantEngine 
from news_manager import NewsManager  
import threading
import math

# ==========================================
# ADVANCED TIERED LOGGING SETUP
# ==========================================
os.makedirs("logs", exist_ok=True)
logger = logging.getLogger("TradeCoreEngine")
logger.setLevel(logging.DEBUG)

if not logger.handlers:
    c_handler = logging.StreamHandler()
    c_handler.setLevel(logging.INFO)
    
    from logging.handlers import RotatingFileHandler
    
    f_handler = RotatingFileHandler(
        "logs/tradecore_brain.log", 
        maxBytes=5*1024*1024, 
        backupCount=5, 
        encoding='utf-8'
    )
    f_handler.setLevel(logging.DEBUG)
    
    fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    c_handler.setFormatter(fmt)
    f_handler.setFormatter(fmt)
    
    logger.addHandler(c_handler)
    logger.addHandler(f_handler)

class TradingBot:
    def __init__(self):
        self.gateway = MT5Gateway()
        self.notifier = TelegramNotifier() 
        self.news_manager = NewsManager() 
        
        # [SPRINT 18a] Threading Lock to prevent MT5 C-extension collisions
        # during asynchronous LIMIT order dispatch vs Fast Loop state checking.
        self.mt5_lock = threading.Lock()
        
        self.vip_assets = [
            "EURUSD", "GBPUSD", "USDJPY",         
            "XAUUSD", "XAGUSD", "US Oil",         
            "US SP 500", "US Tech 100",           
            "BTCUSD", "ETHUSD",                   
        ]
        
        self.active_symbols = [] 
        self.symbol_cooldowns = {} 
        
        self.MAX_OPEN_TRADES = 12       
        self.MAX_SNIPER_SLOTS = 5      
        self.MAX_GOLD_TRADES = 3       
        
        self.logs = []
        self.is_running = False
        self.active_tickets = set()
        self.execution_lock = set() 
        
        # [SPRINT 18a] Close-Spam Protection Set
        self._closing_tickets = set()
        
        self.state_file = "logs/tradecore_state.json"
        self.scaled_positions = set()  
        
        self.daily_start_balance = 0.0
        self.last_trade_day = -1
        self.kill_switch_active = False
        self.kill_switch_time   = None    

        self.current_var = 0.0
        self.market_regime = "CALIBRATING..."

        self._last_logged_signal: dict = {}
        self._pending_order_info: dict = {}
        self._price_close_rejections: dict = {}
        self._stale_zone_cooldowns: dict = {}
        self._stale_trap_counts: dict = {}
        self._momentum_watch: dict = {}
        self._last_markov_gate: str = "OK"  
        self._manual_pause_until: datetime = None

        self.quant_engine = QuantEngine()
        self._risk_reduction_mode = False

    def _get_current_account_id(self):
        try:
            return self.gateway.get_account_id()
        except Exception:
            return None

    def _load_state(self):
        try:
            if os.path.exists(self.state_file):
                with open(self.state_file, "r") as f:
                    data = json.load(f)
                saved_account   = data.get("account_id")
                current_account = self._get_current_account_id()
                if saved_account and current_account and saved_account != current_account:
                    self.log_info(
                        f"⚠️  Account switch detected: saved={saved_account} "
                        f"current={current_account}. State cleared for new account."
                    )
                    self._save_state()
                    return set()
                raw_dedup = data.get("last_logged_signal", {})
                self._last_logged_signal = {k: tuple(v) for k, v in raw_dedup.items()}
                return set(data.get("scaled_positions", []))
        except Exception as e:
            self.log_debug(f"State Load Error: {e}")
        return set()

    def _save_state(self):
        try:
            account_id = self._get_current_account_id()
            dedup_serializable = {
                k: list(v) for k, v in self._last_logged_signal.items()
            }
            with open(self.state_file, "w") as f:
                json.dump({
                    "account_id":          account_id,
                    "scaled_positions":    list(self.scaled_positions),
                    "last_logged_signal":  dedup_serializable,
                }, f, indent=2)
        except Exception as e:
            self.log_debug(f"State Save Error: {e}")

    def _garbage_collect_state(self):
        """
        [SPRINT 18a] State-Memory Leak Fix.
        Purges dead price coordinates and expired cooldowns at daily rollover.
        Ensures Ram footprint remains pristine over months of 24/5 uptime.
        """
        now = datetime.utcnow()
        
        # 1. Prune stale zone cooldowns > 24h
        stale_keys = [k for k, t in self._stale_zone_cooldowns.items() if (now - t).total_seconds() > 86400]
        for k in stale_keys:
            self._stale_zone_cooldowns.pop(k, None)

        # 2. Prune symbol cooldowns > 24h
        sym_keys = [k for k, t in self.symbol_cooldowns.items() if (now - t).total_seconds() > 86400]
        for k in sym_keys:
            self.symbol_cooldowns.pop(k, None)
            
        # 3. Wipe static counters daily (zones from yesterday are structurally irrelevant)
        self._stale_trap_counts.clear()
        self._price_close_rejections.clear()
        
        # 4. Prune momentum watch list > 24h
        mom_keys = [k for k, v in self._momentum_watch.items() if (now - v['fill_time']).total_seconds() > 86400]
        for k in mom_keys:
            self._momentum_watch.pop(k, None)
            
        self.log_debug("🧹 Garbage Collection: Cleared stale state tracking variables.")

    def log_info(self, message):
        logger.info(message)
        timestamp = datetime.now().strftime("%H:%M:%S")
        entry = f"[{timestamp}] {message}"
        self.logs.insert(0, entry)
        if len(self.logs) > 100: 
            self.logs.pop()

    def log_debug(self, message):
        logger.debug(message)

    def async_alert(self, msg):
        def _send():
            try: 
                self.notifier.send(msg)
            except: 
                pass
        threading.Thread(target=_send).start()

    def handle_telegram_command(self, command):
        parts = command.strip().split()
        cmd   = parts[0].lower() if parts else ""
        args  = parts[1:] if len(parts) > 1 else []
        self.log_info(f"📩 Received Command: {cmd}")

        if cmd == "/help":
            msg = (
                "🤖 **Kom v1.0 — Commands**\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "📊 *Info*\n"
                "  /status — live balance, positions, float PnL\n"
                "  /balance — quick balance check\n"
                "  /performance — all-time win rate, P&L, R/R\n"
                "  /risk — VaR, Kelly %, daily P&L, drawdown\n"
                "  /regime — current Markov market state\n"
                "  /amd [SYM] — AMD phase for all or one asset\n"
                "  /news — upcoming high-impact events\n"
                "  /trades [N] — last N closed trades (default 5)\n"
                "  /signals [N] — last N signals placed (default 5)\n"
                "  /pending — active limit orders\n"
                "  /cooldowns — active symbol cooldowns\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "⚙️ *Control*\n"
                "  /pause [mins] — pause new signals (default 30m)\n"
                "  /resume — resume signal execution immediately\n"
                "  /close [SYMBOL|all] — close position(s)\n"
                "  /cancel [SYMBOL|all] — cancel pending limit(s)\n"
                "  /summary — trigger daily summary report now\n"
                "  /stop — stop the engine entirely\n"
                "  /start — restart the engine\n"
            )
            self.async_alert(msg)
        elif cmd == "/status":
            self._report_status()
        elif cmd == "/balance":
            acc = self.gateway.get_account_info()
            if acc:
                self.async_alert(
                    f"💰 **Balance:** ${acc['balance']:,.2f}\n"
                    f"📈 **Equity:** ${acc['equity']:,.2f}\n"
                    f"🛡️ **Free Margin:** ${acc['free_margin']:,.2f}"
                )
        elif cmd == "/performance":
            try:
                import sqlite3 as _sl
                con = _sl.connect("tradecore.db")
                rows = con.execute("""
                    SELECT profit FROM trades
                    WHERE profit IS NOT NULL AND profit != 0
                      AND comment NOT LIKE '%ghost%'
                """).fetchall()
                con.close()
                profits = [r[0] for r in rows]
                if not profits:
                    self.async_alert("📉 No closed trades yet.")
                    return
                wins   = [p for p in profits if p > 0]
                losses = [p for p in profits if p < 0]
                wr     = len(wins) / len(profits) * 100
                net    = sum(profits)
                avg_w  = sum(wins) / len(wins) if wins else 0
                avg_l  = abs(sum(losses) / len(losses)) if losses else 1
                rr     = avg_w / avg_l if avg_l > 0 else 0
                pf     = sum(wins) / abs(sum(losses)) if losses else float('inf')
                max_dd = min(profits)
                acc    = self.gateway.get_account_info()
                bal    = acc['balance'] if acc else 0
                self.async_alert(
                    f"📊 **All-Time Performance**\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"💰 Net P&L:    ${net:+,.2f}\n"
                    f"📈 Balance:    ${bal:,.2f}\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"🎯 Trades:     {len(profits)} (W:{len(wins)} L:{len(losses)})\n"
                    f"✅ Win Rate:   {wr:.1f}%\n"
                    f"⚖️ R/R Ratio:  {rr:.2f}:1\n"
                    f"📊 Profit Fac: {pf:.2f}\n"
                    f"📉 Max Loss:   ${max_dd:+.2f}\n"
                    f"💵 Avg Win:    ${avg_w:+.2f}\n"
                    f"🔴 Avg Loss:   ${-avg_l:+.2f}\n"
                    f"🎲 E[trade]:   ${net/len(profits):+.2f}"
                )
            except Exception as e:
                self.async_alert(f"⚠️ Performance query error: {e}")
        elif cmd == "/trades":
            try:
                n = int(args[0]) if args else 5
                n = max(1, min(n, 20))
                import sqlite3 as _sl
                con = _sl.connect("tradecore.db")
                rows = con.execute("""
                    SELECT symbol, type, profit, volume, close_time FROM trades
                    WHERE profit IS NOT NULL AND profit != 0
                      AND comment NOT LIKE '%ghost%'
                    ORDER BY close_time DESC LIMIT ?
                """, (n,)).fetchall()
                con.close()
                if not rows:
                    self.async_alert("📭 No closed trades found.")
                    return
                lines = [f"📋 **Last {n} Closed Trades**\n━━━━━━━━━━━━━━━━━━━━"]
                for sym, typ, pnl, vol, ct in rows:
                    icon = "🟢" if pnl > 0 else "🔴"
                    dt   = str(ct)[:16] if ct else "—"
                    lines.append(f"{icon} {sym} {typ} | ${pnl:+.2f} | {vol}L | {dt}")
                self.async_alert("\n".join(lines))
            except Exception as e:
                self.async_alert(f"⚠️ Trades query error: {e}")
        elif cmd == "/pending":
            try:
                import MetaTrader5 as _mt5
                orders = _mt5.orders_get()
                if not orders:
                    self.async_alert("📭 No pending limit orders.")
                    return
                lines = [f"⏳ **Pending Orders ({len(orders)})**\n━━━━━━━━━━━━━━━━━━━━"]
                for o in orders:
                    typ  = "BUY_LIM" if o.type == _mt5.ORDER_TYPE_BUY_LIMIT else "SELL_LIM"
                    age  = int((datetime.utcnow().timestamp() - o.time_setup) / 60)
                    lines.append(f"📌 {o.symbol} {typ} @ {o.price_open:.5g} | {o.volume_current}L | {age}m ago")
                self.async_alert("\n".join(lines))
            except Exception as e:
                self.async_alert(f"⚠️ Pending orders error: {e}")
        elif cmd == "/close":
            target = args[0].upper() if args else "all"
            positions = self.gateway.get_open_positions()
            if not positions:
                self.async_alert("📭 No open positions to close.")
                return
            closed, failed = [], []
            for pos in positions:
                if target == "ALL" or pos['symbol'].upper() == target or \
                   target in pos['symbol'].upper():
                    ok = self.gateway.close_position(
                        pos['ticket'], pos['symbol'], pos['volume'], pos['type']
                    )
                    (closed if ok else failed).append(pos['symbol'])
            parts_msg = []
            if closed:  parts_msg.append(f"✅ Closed: {', '.join(closed)}")
            if failed:  parts_msg.append(f"❌ Failed: {', '.join(failed)}")
            if not closed and not failed:
                parts_msg.append(f"⚠️ No positions found matching '{target}'")
            self.async_alert("🔒 **Manual Close**\n" + "\n".join(parts_msg))
        elif cmd == "/cancel":
            target = args[0].upper() if args else "all"
            try:
                import MetaTrader5 as _mt5
                orders = _mt5.orders_get()
                if not orders:
                    self.async_alert("📭 No pending orders to cancel.")
                    return
                cancelled, failed = [], []
                for o in orders:
                    sym = o.symbol.upper()
                    if target == "ALL" or target in sym or sym == target:
                        req = {
                            "action": _mt5.TRADE_ACTION_REMOVE,
                            "order":  o.ticket,
                        }
                        with self.mt5_lock:
                            res = _mt5.order_send(req)
                        if res and res.retcode == _mt5.TRADE_RETCODE_DONE:
                            cancelled.append(o.symbol)
                            self.execution_lock.discard(o.symbol)
                        else:
                            failed.append(o.symbol)
                parts_msg = []
                if cancelled: parts_msg.append(f"✅ Cancelled: {', '.join(cancelled)}")
                if failed:    parts_msg.append(f"❌ Failed: {', '.join(failed)}")
                self.async_alert("🗑️ **Manual Cancel**\n" + "\n".join(parts_msg))
            except Exception as e:
                self.async_alert(f"⚠️ Cancel error: {e}")
        elif cmd == "/regime":
            regime = self.market_regime
            ks     = self.kill_switch_active
            paused = (
                self._manual_pause_until and
                datetime.utcnow() < self._manual_pause_until
            )
            pause_txt = ""
            if paused:
                remaining = int((self._manual_pause_until - datetime.utcnow()).total_seconds() / 60)
                pause_txt = f"\n⏸️ **Signals paused** — {remaining}m remaining"
            markov_gate = self._last_markov_gate
            icon = "🟢" if "NORMAL" in regime else ("🟡" if "HIGH" in regime else "⚪")
            self.async_alert(
                f"{icon} **Market Regime**\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"Regime:      {regime}\n"
                f"Markov Gate: {markov_gate}\n"
                f"Kill Switch: {'🛑 ACTIVE' if ks else '✅ Clear'}"
                f"{pause_txt}"
            )
        elif cmd == "/pause":
            try:
                mins = int(args[0]) if args else 30
                mins = max(1, min(mins, 480))
                self._manual_pause_until = datetime.utcnow() + timedelta(minutes=mins)
                self.async_alert(
                    f"⏸️ **Signals Paused for {mins} minutes**\n"
                    f"Resumes at {self._manual_pause_until.strftime('%H:%M')} UTC\n"
                    f"Use /resume to cancel early."
                )
            except Exception:
                self.async_alert("⚠️ Usage: /pause [minutes] (e.g. /pause 60)")
        elif cmd == "/resume":
            self._manual_pause_until = None
            self.async_alert("▶️ **Signal execution resumed.**")
        elif cmd == "/news":
            news_data = self.news_manager.get_upcoming_news()
            if not news_data:
                self.async_alert("🌍 **No High Impact News Found.**")
            else:
                lines = ["📰 **Upcoming News Risks**\n━━━━━━━━━━━━━━━━━━━━"]
                for item in news_data[:8]:
                    icon = "🔴" if item['impact'] == 'High' else "🟠"
                    lines.append(
                        f"{icon} {item['time'].split()[-1]} • {item['country']} {item['title']}"
                    )
                self.async_alert("\n".join(lines))
        elif cmd == "/summary":
            self.async_alert("📋 Generating daily summary...")
            self.send_daily_summary()
        elif cmd == "/risk":
            try:
                import sqlite3 as _sl
                acc = self.gateway.get_account_info()
                bal = acc['balance'] if acc else 0
                eq  = acc['equity']  if acc else 0
                con = _sl.connect("tradecore.db")
                today = datetime.utcnow().strftime('%Y-%m-%d')
                rows_today = con.execute(
                    "SELECT SUM(profit) FROM trades WHERE close_time LIKE ? AND profit IS NOT NULL",
                    (f"{today}%",)
                ).fetchone()
                all_profits = con.execute(
                    "SELECT profit FROM trades WHERE profit IS NOT NULL AND profit != 0"
                ).fetchall()
                con.close()
                daily_pnl  = rows_today[0] or 0.0
                profits    = [r[0] for r in all_profits]
                peak       = bal
                cumsum     = 0.0
                for p in profits:
                    cumsum += p
                    peak    = max(peak, bal - cumsum + p) if p else peak
                drawdown   = ((peak - bal) / peak * 100) if peak > 0 else 0
                try:
                    from quant_analyzer import QuantAnalyzer
                    qa = QuantAnalyzer(profits)
                    kelly = qa.kelly_fraction * 100
                    var95 = qa.value_at_risk(0.95)
                except Exception:
                    kelly, var95 = 0.0, 0.0
                weekly_target = 3000.0  
                days_remaining = 7 - datetime.utcnow().weekday()
                self.async_alert(
                    f"🔬 **Risk Dashboard**\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"💰 Balance:       ${bal:,.2f}\n"
                    f"📊 Equity:        ${eq:,.2f}\n"
                    f"📅 Today P&L:     ${daily_pnl:+.2f}\n"
                    f"📉 Drawdown:      {drawdown:.1f}%\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"🎲 Kelly Fraction: {kelly:.1f}%\n"
                    f"📐 VaR(95%):      ${var95:.2f}\n"
                    f"🎯 Weekly Target: ${weekly_target:,.0f} ({days_remaining}d left)\n"
                    f"🔢 Total Trades:  {len(profits)}/30 (Kelly activates at 30)"
                )
            except Exception as e:
                self.async_alert(f"⚠️ Risk query error: {e}")
        elif cmd == "/amd":
            try:
                from analyst import compute_amd_phase, SESSION_WINDOWS
                target_sym = args[0].upper() if args else None
                syms = ([target_sym] if target_sym else self.active_symbols[:10])
                import MetaTrader5 as _mt5
                import pandas as _pd
                lines = ["🔄 **AMD Phase Report**\n━━━━━━━━━━━━━━━━━━━━"]
                for sym in syms:
                    try:
                        _rates = _mt5.copy_rates_from_pos(
                            self.gateway.find_symbol(sym) or sym,
                            _mt5.TIMEFRAME_M15, 0, 200
                        )
                        if _rates is None:
                            lines.append(f"⚪ {sym}: no data")
                            continue
                        df = _pd.DataFrame(_rates)
                        df['time'] = _pd.to_datetime(df['time'], unit='s')
                        df.set_index('time', inplace=True)
                        phase, conf, _ = compute_amd_phase(df)
                        icon = {"ACCUMULATION": "🟡", "MANIPULATION": "🟠",
                                "DISTRIBUTION": "🟢"}.get(phase, "⚪")
                        lines.append(f"{icon} {sym}: {phase} ({conf:.0%})")
                    except Exception:
                        lines.append(f"⚪ {sym}: error")
                self.async_alert("\n".join(lines))
            except Exception as e:
                self.async_alert(f"⚠️ AMD query error: {e}")
        elif cmd == "/cooldowns":
            try:
                now = datetime.utcnow()
                lines = ["⏱️ **Active Cooldowns**\n━━━━━━━━━━━━━━━━━━━━"]
                sym_cds = [(sym, t) for sym, t in self.symbol_cooldowns.items()
                           if (now - t).total_seconds() < 900]
                if sym_cds:
                    for sym, t in sorted(sym_cds, key=lambda x: x[1], reverse=True):
                        remaining = 900 - int((now - t).total_seconds())
                        lines.append(f"🔴 {sym}: {remaining//60}m {remaining%60}s left")
                sz_cds = [(k, t) for k, t in self._stale_zone_cooldowns.items()
                          if (now - t).total_seconds() < 900]
                if sz_cds:
                    for (sym, zone), t in sz_cds:
                        remaining = 900 - int((now - t).total_seconds())
                        lines.append(f"🚫 {sym} zone {zone}: {remaining//60}m left (stale OB)")
                if self._stale_trap_counts:
                    for (sym, px), cnt in self._stale_trap_counts.items():
                        lines.append(f"⚠️ {sym} @ {px}: {cnt}/3 stale traps")
                if len(lines) == 1:
                    lines.append("✅ No active cooldowns — all symbols ready.")
                self.async_alert("\n".join(lines))
            except Exception as e:
                self.async_alert(f"⚠️ Cooldown query error: {e}")
        elif cmd == "/signals":
            try:
                n = int(args[0]) if args else 5
                n = max(1, min(n, 15))
                import sqlite3 as _sl
                con = _sl.connect("tradecore.db")
                rows = con.execute("""
                    SELECT symbol, signal_type, confidence, entry_price, timestamp
                    FROM signals
                    WHERE entry_price IS NOT NULL
                    ORDER BY timestamp DESC LIMIT ?
                """, (n,)).fetchall()
                con.close()
                if not rows:
                    self.async_alert("📭 No recent signals in DB.")
                    return
                lines = [f"📡 **Last {n} Signals**\n━━━━━━━━━━━━━━━━━━━━"]
                for sym, sig, conf, ep, ts in rows:
                    conf_pct = f"{float(conf)*100:.0f}%" if conf else "—"
                    ep_str   = f"@ {float(ep):.5g}" if ep else ""
                    dt       = str(ts)[:16] if ts else "—"
                    lines.append(f"📌 {sym} {sig} {conf_pct} {ep_str} | {dt}")
                self.async_alert("\n".join(lines))
            except Exception as e:
                self.async_alert(f"⚠️ Signals query error: {e}")
        elif cmd == "/stop":
            self.stop_service()
            self.async_alert("🛑 **Bot Stopped by User Command**")
        elif cmd == "/start":
            if not self.is_running:
                self.start_service()
        else:
            self.async_alert(
                f"❓ Unknown command: `{cmd}`\n"
                f"Send /help to see all available commands."
            )

    def _report_status(self):
        acc = self.gateway.get_account_info()
        if acc:
            balance      = acc['balance']
            equity       = acc['equity']
            free_margin  = acc['free_margin']
            margin_level = acc.get('margin_level', 0.0)
        else:
            balance = equity = free_margin = margin_level = 0.0

        positions    = self.gateway.get_open_positions()
        total_profit = sum(p['profit'] for p in positions)

        today_pnl   = 0.0
        today_w     = 0
        today_l     = 0
        weekly_pnl  = 0.0
        total_trades = 0
        try:
            import sqlite3 as _sl
            today_str      = datetime.utcnow().strftime('%Y-%m-%d')
            from datetime import timedelta as _td
            week_start_str = (datetime.utcnow() - _td(days=datetime.utcnow().weekday())).strftime('%Y-%m-%d')
            con = _sl.connect("tradecore.db")
            today_rows  = con.execute(
                "SELECT profit FROM trades WHERE close_time LIKE ? AND profit IS NOT NULL AND profit != 0",
                (f"{today_str}%",)
            ).fetchall()
            week_rows   = con.execute(
                "SELECT profit FROM trades WHERE close_time >= ? AND profit IS NOT NULL AND profit != 0 AND comment NOT LIKE '%ghost%'",
                (week_start_str,)
            ).fetchall()
            all_rows    = con.execute(
                "SELECT COUNT(*) FROM trades WHERE profit IS NOT NULL AND profit != 0 AND comment NOT LIKE '%ghost%'"
            ).fetchone()
            con.close()
            today_pnl    = sum(r[0] for r in today_rows)
            today_w      = sum(1 for r in today_rows if r[0] > 0)
            today_l      = sum(1 for r in today_rows if r[0] < 0)
            weekly_pnl   = sum(r[0] for r in week_rows)
            total_trades = all_rows[0] if all_rows else 0
        except Exception:
            pass

        WEEKLY_TARGET   = 3000.0
        wp_pct          = min(100.0, (weekly_pnl / WEEKLY_TARGET) * 100) if WEEKLY_TARGET > 0 else 0
        filled_bars     = min(10, int(wp_pct / 10))
        bar             = "█" * filled_bars + "░" * (10 - filled_bars)
        week_icon       = "🎯" if weekly_pnl >= WEEKLY_TARGET else ("🟢" if weekly_pnl > 0 else "🔴")

        raw_orders   = mt5.orders_get()
        pending_list = list(raw_orders) if raw_orders else []

        regime      = self.market_regime
        ks          = self.kill_switch_active
        markov_gate = self._last_markov_gate
        regime_icon = "🟢" if "NORMAL" in regime else ("🟡" if "HIGH" in regime or "NEUTRAL" in regime else "🔴")
        paused      = self._manual_pause_until and datetime.utcnow() < self._manual_pause_until
        pause_line  = ""
        if paused:
            mins_left  = int((self._manual_pause_until - datetime.utcnow()).total_seconds() / 60)
            pause_line = f"\n⏸️ PAUSED — {mins_left}m remaining"
        ks_line     = "\n🛑 KILL SWITCH ACTIVE" if ks else ""

        now = datetime.utcnow()
        live_cds = [sym for sym, t in self.symbol_cooldowns.items()
                    if (now - t).total_seconds() < 900]
        cd_line  = f"\n⏱️ Cooldowns: {', '.join(live_cds)}" if live_cds else ""

        kelly_bar = f"{total_trades}/30" if total_trades < 30 else "✅ ACTIVE"

        msg = (
            f"📊 **Kom v1.0** {datetime.utcnow().strftime('%H:%M UTC')}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Balance:    ${balance:,.2f}\n"
            f"📈 Equity:     ${equity:,.2f}  (float: ${total_profit:+.2f})\n"
            f"🛡️ Margin:     ${free_margin:,.2f}  ({margin_level:.0f}%)\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📅 Today:      ${today_pnl:+.2f}  ({today_w}W / {today_l}L)\n"
            f"{week_icon} Week [{bar}] {wp_pct:.0f}%\n"
            f"   ${weekly_pnl:+.2f} of ${WEEKLY_TARGET:,.0f} target\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 Open:       {len(positions)}  |  ⏳ Pending: {len(pending_list)}\n"
            f"🔢 Kelly:      {kelly_bar}\n"
            f"{regime_icon} Regime:    {regime}\n"
            f"🧠 Markov:     {markov_gate}"
            f"{pause_line}{ks_line}{cd_line}\n"
        )

        if positions:
            msg += "━━━━━━━━━━━━━━━━━━━━\n"
            for p in positions:
                icon   = "🟢" if p['profit'] >= 0 else "🔴"
                dur_h  = (time.time() - p.get('time', time.time())) / 3600
                dur_s  = f"{dur_h:.1f}h"
                msg   += f"{icon} {p['symbol']} {p['type']}: ${p['profit']:+.2f}  ({dur_s})\n"

        if pending_list:
            msg += "━━━━━━━━━━━━━━━━━━━━\n"
            for o in pending_list:
                typ  = "BUY↑" if o.type == mt5.ORDER_TYPE_BUY_LIMIT else "SELL↓"
                age  = int((datetime.utcnow().timestamp() - o.time_setup) / 60)
                msg += f"📌 {o.symbol} {typ} @ {o.price_open:.5g}  ({age}m)\n"

        msg += "━━━━━━━━━━━━━━━━━━━━\n/help for full command list"
        self.async_alert(msg)

    def send_daily_summary(self):
        try:
            acc = self.gateway.get_account_info()
            balance  = acc['balance']  if acc else 0.0
            equity   = acc['equity']   if acc else 0.0
            day_pnl  = balance - self.daily_start_balance

            positions = self.gateway.get_open_positions()
            floating  = sum(p['profit'] for p in positions)

            today_str = datetime.utcnow().strftime('%Y-%m-%d')
            try:
                import sqlite3 as _sl
                con = _sl.connect("tradecore.db")
                rows = con.execute("""
                    SELECT result, COUNT(*) FROM signals
                    WHERE timestamp >= ? GROUP BY result
                """, (today_str,)).fetchall()
                con.close()
                funnel = {r[0]: r[1] for r in rows}
            except Exception:
                funnel = {}

            filled    = funnel.get('FILLED', 0)
            attempted = funnel.get('ATTEMPTED', 0)
            rejected  = sum(v for k, v in funnel.items() if 'REJECTED' in k)
            low_conf  = sum(v for k, v in funnel.items() if 'LOW_CONFIDENCE' in k)
            skipped   = funnel.get('SKIPPED', 0)

            WEEKLY_TARGET = 3000.0
            weekly_pnl = 0.0
            trades_this_week = 0
            winners_this_week = 0
            try:
                import sqlite3 as _sl
                from datetime import timedelta
                now_for_week = datetime.utcnow()
                week_start = now_for_week - timedelta(days=now_for_week.weekday())
                week_start_str = week_start.strftime('%Y-%m-%d')
                con2 = _sl.connect("tradecore.db")
                week_trades = con2.execute("""
                    SELECT profit FROM trades
                    WHERE close_time >= ? AND profit IS NOT NULL AND profit != 0
                      AND comment NOT LIKE '%ghost%'
                """, (week_start_str,)).fetchall()
                con2.close()
                weekly_pnl = sum(r[0] for r in week_trades)
                trades_this_week = len(week_trades)
                winners_this_week = sum(1 for r in week_trades if r[0] > 0)
            except Exception:
                pass

            week_progress_pct = (weekly_pnl / WEEKLY_TARGET) * 100 if WEEKLY_TARGET > 0 else 0
            remaining_to_target = WEEKLY_TARGET - weekly_pnl
            filled_bars = min(10, int(week_progress_pct / 10))
            bar = "█" * filled_bars + "░" * (10 - filled_bars)
            week_icon = "🎯" if weekly_pnl >= WEEKLY_TARGET else ("🟢" if weekly_pnl > 0 else "🔴")
            week_wr = f"{winners_this_week}/{trades_this_week}" if trades_this_week > 0 else "—"

            news_lines = []
            now = datetime.utcnow()
            for ev in self.news_manager.events:
                edt = ev.get('_event_dt')
                if edt and ev['impact'] == 'High':
                    hrs = (edt - now).total_seconds() / 3600
                    if 0 < hrs < 12:
                        icon = "🔴" if ev.get('tier') == 1 else "🟠"
                        news_lines.append(
                            f"{icon} {ev['country']} {ev['title']} (in {hrs:.1f}h)"
                        )

            ks_status  = "🛑 ACTIVE — bot halted" if self.kill_switch_active else "✅ Clear"
            regime_ico = "🟢" if "NORMAL" in self.market_regime else ("🟡" if "HIGH" in self.market_regime else "⚪")

            msg = (
                f"📋 **Kom v1.0 — Daily Summary**\n"
                f"🕐 UTC {now.strftime('%Y-%m-%d %H:%M')}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"💰 Balance:   ${balance:,.2f}\n"
                f"📈 Equity:    ${equity:,.2f}\n"
                f"📊 Day P&L:   ${day_pnl:+.2f}  (float: ${floating:+.2f})\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"{week_icon} **Weekly Target: $3,000**\n"
                f"   [{bar}] {week_progress_pct:.0f}%\n"
                f"   Earned: ${weekly_pnl:+.2f}  |  Need: ${remaining_to_target:+.2f}\n"
                f"   Trades: {week_wr} W/L this week\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📡 Signal Funnel (today)\n"
                f"   Fills:       {filled}\n"
                f"   Attempted:   {attempted}\n"
                f"   Rejected:    {rejected}\n"
                f"   Low Conf:    {low_conf}\n"
                f"   Skipped:     {skipped}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"{regime_ico} Regime: {self.market_regime}\n"
                f"🔒 Kill Switch: {ks_status}\n"
            )

            if news_lines:
                msg += "━━━━━━━━━━━━━━━━━━━━\n📰 Upcoming News (12h)\n"
                msg += "\n".join(news_lines[:5])

            if positions:
                msg += "\n━━━━━━━━━━━━━━━━━━━━\n🎯 Open Positions\n"
                for p in positions:
                    icon = "🟢" if p['profit'] >= 0 else "🔴"
                    msg += f"{icon} {p['symbol']} {p['type']}: ${p['profit']:+.2f}\n"

            self.async_alert(msg)
            self.log_info("📋 Daily summary dispatched via Telegram.")

        except Exception as e:
            self.log_debug(f"Daily Summary Error: {e}")

    def start_service(self):
        if not self.gateway.start():
            self.log_info("CRITICAL: MT5 Connection Failed")
            return False

        try:
            import sqlite3 as _sl
            _con = _sl.connect("tradecore.db")
            _con.execute("PRAGMA journal_mode=WAL")
            _con.execute("PRAGMA synchronous=NORMAL")
            _con.execute("PRAGMA wal_checkpoint(FULL)")
            _ic = _con.execute("PRAGMA integrity_check").fetchone()
            _con.close()
            if _ic and _ic[0] == "ok":
                self.log_info("✅ DB integrity check passed (WAL checkpointed).")
            else:
                self.log_info(f"⚠️ DB integrity warning on startup: {_ic}. Trade logging may be impaired.")
        except Exception as _e:
            self.log_info(f"⚠️ DB startup check error: {_e}. Continuing without DB.")
        
        self.active_symbols = []
        for v in self.vip_assets:
            real = self.gateway.find_symbol(v)
            if real: 
                self.active_symbols.append(real)
                mt5.symbol_select(real, True)

        self.is_running = True
        self.execution_lock.clear()
        self.account_id = self._get_current_account_id()
        self.scaled_positions = self._load_state()
        self.log_info(f"✅ Kom v1.0: Engine Active. Monitoring {len(self.active_symbols)} Elite Assets.")
        
        self.news_manager.fetch_calendar()
        self.notifier.start_listening(self.handle_telegram_command)
        self.async_alert("🚀 **Kom v1.0 Master Online**\nDynamic Structural Targets Armed.")
        return True

    def stop_service(self):
        self.is_running = False
        self.notifier.stop_listening()
        try:
            import sqlite3 as _sl
            _con = _sl.connect("tradecore.db")
            _con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            _con.close()
        except Exception:
            pass
        self.log_info("Stopped.")

    def check_market_schedule(self):
        now = datetime.utcnow()
        day = now.weekday() 
        hour = now.hour
        minute = now.minute

        if day == 4 and (hour > 21 or (hour == 21 and minute >= 50)): 
            return False, "Weekend Close Phase"
        if day == 5: 
            return False, "Weekend Closed"
        if day == 6 and (hour < 22 or (hour == 22 and minute <= 5)): 
            return False, "Sunday Open Phase"
        if (hour == 21 and minute >= 50) or (hour == 22 and minute <= 10): 
            return False, "Daily Rollover (Danger Zone)"

        return True, "Market Open"

    def close_all_positions(self, positions):
        for pos in positions:
            self.gateway.close_position(pos['ticket'], pos['symbol'], pos['volume'], pos['type'])

    def evaluate_pending_orders(self):
        orders = mt5.orders_get()
        if not orders: 
            return
        
        for ord in orders:
            symbol = ord.symbol
            ticket = ord.ticket
            order_type = ord.type
            tp = ord.tp
            sl = ord.sl
            
            tick = mt5.symbol_info_tick(symbol)
            if not tick: 
                continue
            
            cancel = False
            reason = ""
            
            if order_type == mt5.ORDER_TYPE_BUY_LIMIT:
                if tick.bid >= tp and tp > 0.0:
                    cancel, reason = True, "Target reached before entry (Stale Trap)"
                elif tick.bid <= sl and sl > 0.0:
                    cancel, reason = True, "Structure invalidated before entry"
            elif order_type == mt5.ORDER_TYPE_SELL_LIMIT:
                if tick.ask <= tp and tp > 0.0:
                    cancel, reason = True, "Target reached before entry (Stale Trap)"
                elif tick.ask >= sl and sl > 0.0:
                    cancel, reason = True, "Structure invalidated before entry"
                    
            if cancel:
                req = {"action": mt5.TRADE_ACTION_REMOVE, "order": ticket}
                # [SPRINT 18a] Wrapped in MT5 Lock
                with self.mt5_lock:
                    res = mt5.order_send(req)
                if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                    self.log_info(f"🗑️ Stale Trap Avoided: Cancelled {symbol} limit order. ({reason})")

                    entry_px  = round(float(ord.price_open), 5)
                    stale_key = (symbol, entry_px)
                    self._stale_trap_counts[stale_key] = \
                        self._stale_trap_counts.get(stale_key, 0) + 1
                    stale_count = self._stale_trap_counts[stale_key]

                    if stale_count >= 3:
                        self._stale_trap_counts.pop(stale_key, None)
                        self.symbol_cooldowns[symbol] = datetime.utcnow()
                        self.log_info(
                            f"🚫 Momentum Chase Detected: {symbol} entry {entry_px} "
                            f"cancelled 3× by stale trap — OB is behind market. "
                            f"15-min cooldown applied."
                        )
                    else:
                        self.log_debug(
                            f"⚠️ Stale trap #{stale_count}/3 for {symbol} @ {entry_px}."
                        )

    def evaluate_open_positions(self, positions):
        for pos in positions:
            try:
                symbol = pos['symbol']
                ticket = pos['ticket']
                is_buy = pos['type'] == 'BUY'
                
                duration_hours = (time.time() - pos['time']) / 3600.0
                profit = pos['profit']
                
                tick = mt5.symbol_info_tick(symbol)
                if tick:
                    open_price = pos.get('open_price', 0.0)
                    is_buy     = pos['type'] == 'BUY'
                    if is_buy:
                        adverse   = max(0.0, open_price - tick.bid)
                        favorable = max(0.0, tick.bid - open_price)
                    else:
                        adverse   = max(0.0, tick.ask - open_price)
                        favorable = max(0.0, open_price - tick.ask)
                    DBManager.update_mae_mfe(ticket, adverse, favorable)

                if duration_hours > 12.0 and profit < 0:
                    # [SPRINT 18a] Close-Spam Lock
                    if ticket in self._closing_tickets:
                        continue
                    self._closing_tickets.add(ticket)
                    
                    self.log_info(f"⏳ Time Decay Killswitch: {symbol} stuck in dead momentum for >12H. Liquidating.")
                    self.gateway.close_position(ticket, symbol, pos['volume'], pos['type'])
                    self.async_alert(f"⏳ **Dead Momentum Liquidated:** {symbol}\nTrade closed early to free up margin.")
                    self.symbol_cooldowns[symbol] = datetime.utcnow()
                    continue

            except Exception:
                pass

    def get_asset_regime(self, symbol: str) -> str:
        try:
            df = self.gateway.get_market_data(symbol, timeframe=mt5.TIMEFRAME_M15)
        except Exception:
            return "NORMAL (TRENDING)"

        if df.empty or len(df) < 15:
            return "NORMAL (TRENDING)"

        s = symbol.upper()
        recent_high = df['high'].iloc[-15:].max()
        recent_low  = df['low'].iloc[-15:].min()
        price       = df['close'].iloc[-1]
        if price == 0:
            return "NORMAL (TRENDING)"
        vol_pct = (recent_high - recent_low) / price

        if "BTC" in s or "ETH" in s:
            if vol_pct > 0.030:  return "HIGH VOLATILITY (GARCH)"
            if vol_pct > 0.005:  return "NORMAL (TRENDING)"
            return "DEAD MARKET"
        elif "XAU" in s or "XAG" in s:
            if vol_pct > 0.010:  return "HIGH VOLATILITY (GARCH)"
            if vol_pct > 0.0015: return "NORMAL (TRENDING)"
            return "DEAD MARKET"
        elif "Oil" in s or "NGAS" in s:
            if vol_pct > 0.020:  return "HIGH VOLATILITY (GARCH)"
            if vol_pct > 0.003:  return "NORMAL (TRENDING)"
            return "DEAD MARKET"
        elif "SP 500" in s or "Tech 100" in s or "Germany" in s:
            if vol_pct > 0.015:  return "HIGH VOLATILITY (GARCH)"
            if vol_pct > 0.002:  return "NORMAL (TRENDING)"
            return "DEAD MARKET"
        elif "JPY" in s:
            if vol_pct > 0.006:  return "HIGH VOLATILITY (GARCH)"
            if vol_pct > 0.0008: return "NORMAL (TRENDING)"
            return "DEAD MARKET"
        else:
            if vol_pct > 0.008:  return "HIGH VOLATILITY (GARCH)"
            if vol_pct > 0.001:  return "NORMAL (TRENDING)"
            return "DEAD MARKET"

    def evaluate_risk_metrics(self, current_positions=None):
        try:
            import MetaTrader5 as mt5
            # [SPRINT 18a] Decouple baseline from EURUSD dependency
            baseline_df = None
            for base_sym in ["EURUSD", "GBPUSD", "US SP 500", "BTCUSD"]:
                real_base = self.gateway.find_symbol(base_sym) or base_sym
                try:
                    df = self.gateway.get_market_data(real_base)
                    if not df.empty and len(df) >= 15:
                        baseline_df = df
                        break
                except Exception:
                    continue
                    
            if baseline_df is None or baseline_df.empty:
                return "CALIBRATING...", 0.0

            recent_high   = baseline_df['high'].iloc[-15:].max()
            recent_low    = baseline_df['low'].iloc[-15:].min()
            current_price = baseline_df['close'].iloc[-1]
            vol_pct       = (recent_high - recent_low) / current_price

            if vol_pct > 0.008:    regime = "HIGH VOLATILITY (GARCH)"
            elif vol_pct > 0.003:  regime = "NORMAL (TRENDING)"
            else:                  regime = "DEAD MARKET"

            acc     = self.gateway.get_account_info()
            balance = acc['balance'] if acc else 10000.0

            baseline_df['atr'] = calculate_atr(baseline_df, period=14)
            atr_baseline  = baseline_df['atr'].iloc[-1]
            base_std    = (atr_baseline / current_price) if current_price > 0 and atr_baseline > 0 else vol_pct

            max_asset_std = base_std
            if current_positions:
                seen = set()
                for pos in current_positions:
                    sym = pos.get('symbol', '')
                    if sym in seen or not sym:
                        continue
                    seen.add(sym)
                    try:
                        df_pos = self.gateway.get_market_data(sym)
                        if not df_pos.empty and len(df_pos) >= 14:
                            df_pos['atr'] = calculate_atr(df_pos, period=14)
                            atr_pos   = df_pos['atr'].iloc[-1]
                            price_pos = df_pos['close'].iloc[-1]
                            if price_pos > 0 and atr_pos > 0:
                                max_asset_std = max(max_asset_std, atr_pos / price_pos)
                    except Exception:
                        pass

            open_pos      = current_positions if current_positions else self.gateway.get_open_positions()
            n_pos         = max(1, len(open_pos))
            z_99          = 2.326
            single_var    = balance * max_asset_std * z_99
            portfolio_var = single_var * (n_pos ** 0.5)
            daily_var_usd = max(balance * 0.015, min(portfolio_var, balance * 0.20))

            return regime, round(daily_var_usd, 2)

        except Exception as e:
            self.log_debug(f"⚠️ VaR Calculation Error: {e}")
            return "UNKNOWN", 0.0

    def run_execution_cycle(self):
        if not self.is_running: 
            return

        acc = self.gateway.get_account_info()
        if not acc: 
            return
            
        current_positions = self.gateway.get_open_positions()

        # [SPRINT 18a] Clean up the closing tickets lock list
        live_tickets = {p['ticket'] for p in current_positions}
        self._closing_tickets.intersection_update(live_tickets)

        # Check pending LIMIT fills
        if self._pending_order_info:
            db_open = DBManager.get_open_trade_tickets()
            for pos in current_positions:
                ticket = pos.get('ticket')
                if ticket in self._pending_order_info and ticket not in db_open:
                    info = self._pending_order_info.pop(ticket)
                    try:
                        raw_ts = pos.get('time', 0)
                        if raw_ts < 946684800:
                            raw_ts = int(time.time())
                        open_time = datetime.utcfromtimestamp(raw_ts).strftime('%Y-%m-%d %H:%M:%S')
                        DBManager.save_trade(
                            ticket     = ticket,
                            symbol     = pos['symbol'],
                            type_op    = pos['type'],
                            vol        = pos.get('volume', 0.0),
                            open_price = pos.get('open_price', 0.0),
                            sl         = pos.get('sl', 0.0),
                            tp         = pos.get('tp', 0.0),
                            time       = open_time,
                            regime     = info.get('regime'),
                            account_id = info.get('account_id'),
                            model_type = info.get('model_type'),
                            model_sizing = info.get('model_sizing'),
                        )
                        sl_d = abs(pos.get('open_price', 0.0) - pos.get('sl', 0.0))
                        self._momentum_watch[ticket] = {
                            'symbol':          pos['symbol'],
                            'type':            pos['type'],
                            'open_price':      pos.get('open_price', 0.0),
                            'sl_dist':         sl_d,
                            'fill_time':       datetime.utcnow(),
                            'checked':         False,
                        }
                        self.log_debug(f"[{pos['symbol']}] Trade recorded: ticket={ticket}")
                    except Exception as e:
                        self.log_debug(f"Fill Record Error ({ticket}): {e}")

        # Real-time trade close detection
        try:
            mt5_live_tickets = {p['ticket'] for p in current_positions}
            db_open_detail   = DBManager.get_open_trades_detail()
            db_open_map      = {t['ticket']: t for t in db_open_detail}

            for db_ticket, db_trade in db_open_map.items():
                if db_ticket in mt5_live_tickets:
                    continue   

                deals = mt5.history_deals_get(position=db_ticket)
                if not deals:
                    continue   

                close_deal = None
                for deal in reversed(deals):
                    if getattr(deal, 'entry', -1) == mt5.DEAL_ENTRY_OUT:
                        close_deal = deal
                        break
                if close_deal is None:
                    close_deal = deals[-1]
                if close_deal is None:
                    continue

                close_time = datetime.utcfromtimestamp(close_deal.time).strftime('%Y-%m-%d %H:%M:%S')
                net_profit = (close_deal.profit + getattr(close_deal, 'swap', 0.0) + getattr(close_deal, 'commission', 0.0))

                DBManager.close_trade(
                    ticket      = db_ticket,
                    close_price = close_deal.price,
                    close_time  = close_time,
                    profit      = net_profit,
                    commission  = getattr(close_deal, 'commission', 0.0),
                )

                outcome = ("WIN" if net_profit > 0 else "BREAK_EVEN" if net_profit == 0 else "LOSS")
                icon    = "🟢" if net_profit > 0 else "🔴"

                open_px   = db_trade.get('open_price', 0.0) or close_deal.price
                direction = 1 if db_trade.get('type') == 'BUY' else -1
                pips_raw  = (close_deal.price - open_px) * direction

                DBManager.update_signal_outcome(db_trade['symbol'], outcome, pips_raw)

                scale_key = f"{db_trade['symbol']}_{open_px}_{db_trade.get('type','BUY')}"
                self.scaled_positions.discard(scale_key)
                self._save_state()

                self.log_info(f"{icon} Trade Closed: #{db_ticket} {db_trade['symbol']} | P&L: ${net_profit:+.2f}")
                self.async_alert(f"{icon} **Trade Closed — {outcome}**\n{db_trade['symbol']} | P&L: ${net_profit:+.2f}")
        except Exception as _ce:
            self.log_debug(f"Close Detection Error: {_ce}")

        # Entry Momentum Guard
        if self._momentum_watch:
            now_utc = datetime.utcnow()
            stale_tickets = []
            for watch_ticket, wdata in list(self._momentum_watch.items()):
                elapsed = (now_utc - wdata['fill_time']).total_seconds()
                if elapsed < 180:   
                    continue
                if wdata.get('checked'):
                    stale_tickets.append(watch_ticket)
                    continue

                self._momentum_watch[watch_ticket]['checked'] = True
                stale_tickets.append(watch_ticket)

                live_pos = next((p for p in current_positions if p.get('ticket') == watch_ticket), None)
                if live_pos is None:
                    continue

                sym       = wdata['symbol']
                is_buy    = wdata['type'] == 'BUY'
                open_px   = wdata['open_price']
                sl_dist   = wdata['sl_dist']
                if sl_dist <= 0:
                    continue

                tick = mt5.symbol_info_tick(self.gateway.find_symbol(sym) or sym)
                if tick is None:
                    continue

                current_px = tick.bid if is_buy else tick.ask
                adverse_move = (open_px - current_px) if is_buy else (current_px - open_px)

                if adverse_move > sl_dist * 0.30:
                    # [SPRINT 18a] Close-Spam Lock
                    if watch_ticket in self._closing_tickets:
                        continue
                    self._closing_tickets.add(watch_ticket)

                    vol = live_pos.get('volume', 0.0)
                    self.log_info(f"⚡ Momentum Guard Exit: {sym} adverse={adverse_move:.5f}")
                    try:
                        self.gateway.close_position(watch_ticket, sym, vol, wdata['type'])
                    except Exception as _mg_e:
                        self.log_debug(f"Momentum Guard close error: {_mg_e}")

            for t in stale_tickets:
                self._momentum_watch.pop(t, None)

        current_day = datetime.utcnow().day
        
        if self.kill_switch_active:
            now_utc         = datetime.utcnow()
            new_trading_day = (current_day != self.last_trade_day)
            hours_since_ks  = ((now_utc - self.kill_switch_time).total_seconds() / 3600.0 if self.kill_switch_time else 99.0)
            if new_trading_day and hours_since_ks >= 8.0:
                self.log_info("🌅 Kill Switch Reset: New trading day + 8h cooldown passed.")
                self.kill_switch_active = False
                self.kill_switch_time   = None
                self.daily_start_balance = acc['balance']
                self.last_trade_day      = current_day
                self._garbage_collect_state()  # [SPRINT 18a]
            else:
                return

        if current_day != self.last_trade_day:
            self.daily_start_balance = acc['balance']
            self.last_trade_day = current_day
            self._garbage_collect_state()  # [SPRINT 18a]

        if self.daily_start_balance > 0 and self.current_var > 0:
            current_dd_usd = self.daily_start_balance - acc['equity']
            quant_params = self.quant_engine.get_live_risk_params()
            cvar_limit   = quant_params.get('cvar_limit', self.current_var * 1.29)
            var_limit    = quant_params.get('var_limit',  self.current_var)

            if current_dd_usd >= cvar_limit:
                self.log_info(f"🛑 KILL SWITCH [CVaR]: Tail-risk limit breached! (DD: ${current_dd_usd:.2f})")
                self.async_alert(f"🛑 **CRITICAL: CVaR BREACHED**\nLiquidating {len(current_positions)} positions.")
                self.close_all_positions(current_positions)
                self.kill_switch_active = True
                self.kill_switch_time   = datetime.utcnow()
                return
            elif current_dd_usd >= var_limit:
                self._risk_reduction_mode = True
            else:
                self._risk_reduction_mode = False

        self.apply_trailing_stop(current_positions)
        self.evaluate_pending_orders() 
        self.evaluate_open_positions(current_positions) 

        # Session End Profit Anchor
        try:
            now_utc = datetime.utcnow()
            hour    = now_utc.hour
            minute  = now_utc.minute
            is_session_close = ((hour == 17 and minute < 5) or (hour == 22 and minute < 5))
            if is_session_close and current_positions:
                for pos in current_positions:
                    try:
                        _sym   = pos['symbol']
                        _tick  = pos.get('ticket')
                        _type  = pos['type']
                        _open  = pos.get('open_price', 0.0)
                        _sl    = pos.get('sl', 0.0)
                        _tp    = pos.get('tp', 0.0)
                        _vol   = pos.get('volume', 0.0)
                        _since = pos.get('time', 0)

                        hours_open = (now_utc - datetime.utcfromtimestamp(_since)).total_seconds() / 3600
                        if hours_open < 4.0:
                            continue  

                        _tick_data = mt5.symbol_info_tick(self.gateway.find_symbol(_sym) or _sym)
                        if not _tick_data:
                            continue

                        _is_buy     = _type == 'BUY'
                        _price_now  = _tick_data.bid if _is_buy else _tick_data.ask
                        _float_pl   = (_price_now - _open) if _is_buy else (_open - _price_now)
                        _sl_dist    = abs(_open - _sl) if _sl else 0

                        if _float_pl <= 0:
                            continue  

                        _tp_dist    = abs(_tp - _open) if _tp else 0
                        _float_r    = _float_pl / _sl_dist if _sl_dist > 0 else 0
                        _tp_r       = _tp_dist / _sl_dist if _sl_dist > 0 else 0
                        near_tp_sess = (_tp_r > 0 and _float_r >= _tp_r - 2.0)
                        long_runner  = (hours_open > 6.0 and _float_r >= 0.5)

                        if near_tp_sess or long_runner:
                            # [SPRINT 18a] Close-Spam Lock
                            if _tick in self._closing_tickets:
                                continue
                            self._closing_tickets.add(_tick)

                            close_ok = self.gateway.close_position(_tick, _sym, _vol, _type)
                            if close_ok:
                                self.log_info(f"🔔 Session Close: {_sym} {_type} banked at +{_float_r:.2f}R")
                    except Exception as _sc_inner:
                        pass
        except Exception as _sc_e:
            pass

    def run_analysis_cycle(self):
        if not self.is_running: 
            return

        _is_manually_paused = (
            self._manual_pause_until is not None and
            datetime.utcnow() < self._manual_pause_until
        )

        acc = self.gateway.get_account_info()
        if not acc: 
            return
            
        DBManager.log_snapshot(acc['balance'], acc['equity'], acc['margin_level'], acc['free_margin'],
                               account_id=acc.get('account_id'))
                               
        if self.kill_switch_active:
            return

        current_positions = self.gateway.get_open_positions()
        self.market_regime, self.current_var = self.evaluate_risk_metrics(current_positions)

        markov = self.quant_engine.markov_regime()
        if markov.get('trading_gate') == 'HALT':
            if datetime.utcnow().second < 5:
                p_bear = markov.get('p_bear_next', 0)
                p_hv   = markov.get('p_high_vol_next', 0)
                self.log_info(f"🔴 Markov Gate HALT: P(BEAR)={p_bear:.0%} P(HIGH_VOL)={p_hv:.0%}. No new positions.")

            p_bear = markov.get('p_bear_next', 0)
            p_hv   = markov.get('p_high_vol_next', 0)
            new_gate = "HALT_BEAR" if p_bear > 0.65 else "HALT_HVOL" if p_hv > 0.50 else "HALT"
            if new_gate != self._last_markov_gate:
                self._last_markov_gate = new_gate
                try:
                    raw_orders = mt5.orders_get()
                    if raw_orders:
                        cancel_types = set()
                        if "BEAR" in new_gate:
                            cancel_types = {mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_BUY_STOP}
                        else:  
                            cancel_types = {mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_SELL_LIMIT,
                                            mt5.ORDER_TYPE_BUY_STOP,  mt5.ORDER_TYPE_SELL_STOP}
                        cancelled = 0
                        for order in raw_orders:
                            if order.type in cancel_types:
                                req = {"action": mt5.TRADE_ACTION_REMOVE, "order": order.ticket}
                                # [SPRINT 18a] Threading Lock
                                with self.mt5_lock:
                                    res = mt5.order_send(req)
                                if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                                    self._pending_order_info.pop(order.ticket, None)
                                    cancelled += 1
                        if cancelled:
                            self.log_info(f"🧹 Markov Cleanup: {cancelled} contra-regime limit(s) cancelled.")
                except Exception:
                    pass
            return
        else:
            if self._last_markov_gate != "OK":
                self._last_markov_gate = "OK"
            if markov.get('trading_gate') == 'REDUCE':
                self._risk_reduction_mode = True

        is_open, market_status = self.check_market_schedule()
        if not is_open:
            return 

        self.active_tickets = {p['symbol'] for p in current_positions}
        gold_trades = len([p for p in current_positions if "XAU" in p['symbol'] or "XAG" in p['symbol']])
        current_count = len(current_positions) + len(self.execution_lock) 
        
        raw_orders = mt5.orders_get()
        pending_list = list(raw_orders) if raw_orders else []
        usd_exposure_base = len([p for p in current_positions if "USD" in p['symbol']]) + len([o for o in pending_list if "USD" in o.symbol])

        base_exposure: dict = {}
        for p in current_positions:
            b = p['symbol'][:3]
            base_exposure[b] = base_exposure.get(b, 0) + 1
        for o in pending_list:
            b = o.symbol[:3]
            base_exposure[b] = base_exposure.get(b, 0) + 1

        if current_count >= (self.MAX_OPEN_TRADES + self.MAX_SNIPER_SLOTS):
            return

        is_sniper_mode = (current_count >= self.MAX_OPEN_TRADES)
        upcoming_news = self.news_manager.get_upcoming_news() if self.news_manager else []

        for symbol in self.active_symbols:
            if _is_manually_paused:
                continue

            if symbol in self.symbol_cooldowns:
                time_since_close = datetime.utcnow() - self.symbol_cooldowns[symbol]  
                if time_since_close < timedelta(minutes=15):
                    continue 
            
            current_usd_locks = len([s for s in self.execution_lock if "USD" in s])
            if "USD" in symbol and (usd_exposure_base + current_usd_locks) >= 2:
                continue

            sym_base = symbol[:3]
            live_base_count = base_exposure.get(sym_base, 0) + (1 if symbol in self.execution_lock else 0)
            if live_base_count >= 2:
                continue

            symbol_trades = len([p for p in current_positions if p['symbol'] == symbol]) + (1 if symbol in self.execution_lock else 0)
            nano_trades = len([p for p in current_positions if p['symbol'] == symbol and p.get('magic', 510000) == 510001])
            
            if "DEAD MARKET" in self.market_regime and nano_trades >= 1:
                continue 

            if "XAU" in symbol or "XAG" in symbol or "Oil" in symbol or "NGAS" in symbol:
                if is_sniper_mode and gold_trades >= (self.MAX_GOLD_TRADES + 1): 
                    continue 
                elif not is_sniper_mode and gold_trades >= self.MAX_GOLD_TRADES: 
                    continue 
            
            if is_sniper_mode and symbol_trades >= 3: 
                continue 
            elif not is_sniper_mode and symbol_trades >= 2: 
                continue 
            
            self.process_symbol(symbol, is_sniper_mode, upcoming_news)

    def apply_trailing_stop(self, positions):
        for pos in positions:
            try:
                symbol = pos['symbol']
                ticket = pos['ticket']
                magic = pos.get('magic', 510000)
                
                if 'open_price' not in pos: 
                    continue

                tick = mt5.symbol_info_tick(symbol)
                if not tick: 
                    continue
                
                props = self.gateway.get_symbol_properties(symbol)
                min_stop_dist = (props.get('stops_level', 0) * props['point']) if props else 0.0
                min_stop_dist += (props['point'] * 10) if props else 0.0 
                
                vol_step = props.get('volume_step', 0.01) if props else 0.01
                min_lot = props.get('min_lot', 0.01) if props else 0.01

                price_current = tick.bid if pos['type'] == 'BUY' else tick.ask
                is_buy = pos['type'] == 'BUY'
                open_price = pos['open_price']
                current_sl = pos.get('sl', 0.0)
                current_vol = pos.get('volume', 0.0)
                
                profit_dist = (price_current - open_price) if is_buy else (open_price - price_current)
                lock_price = 0.0

                sl_dist_dynamic = abs(open_price - current_sl) if current_sl and current_sl != 0.0 else 0.0

                if sl_dist_dynamic <= 0:
                    if   "XAU" in symbol or "XAG" in symbol or "Oil" in symbol or "NGAS" in symbol:
                        sl_dist_dynamic = 2.0
                    elif "BTC" in symbol or "ETH" in symbol or "US SP 500" in symbol or "US Tech 100" in symbol or "Germany" in symbol:
                        sl_dist_dynamic = 50.0
                    elif "JPY" in symbol:
                        sl_dist_dynamic = 0.200
                    else:
                        sl_dist_dynamic = 0.0050  

                scale_key = f"{symbol}_{open_price}_{pos['type']}"
                is_ready_to_scale = profit_dist >= sl_dist_dynamic  

                if is_ready_to_scale and scale_key not in self.scaled_positions:
                    half_vol = current_vol / 2.0
                    close_vol = round(half_vol / vol_step) * vol_step
                    
                    if close_vol >= min_lot:
                        # [SPRINT 18a] Close-Spam protection for scale outs
                        if f"{ticket}_SCALE" not in self._closing_tickets:
                            self._closing_tickets.add(f"{ticket}_SCALE")
                            self.log_info(f"⚖️ Scaling Out: {symbol} hit 1:1 RR. Closing {close_vol} Lots to secure cash.")
                            success = self.gateway.close_position(ticket, symbol, close_vol, pos['type'])
                            
                            if success:
                                self.scaled_positions.add(scale_key)
                                self._save_state()
                                self.async_alert(f"⚖️ **Partial Take Profit:** {symbol}\nSecured 50% Volume. Moving SL to Breakeven.")
                                breakeven_buffer = props['point'] * 5 
                                lock_price = open_price + breakeven_buffer if is_buy else open_price - breakeven_buffer

                                try:
                                    two_r_tp = (open_price + sl_dist_dynamic * 2.0 if is_buy
                                                else open_price - sl_dist_dynamic * 2.0)
                                    two_r_tp = self.gateway.normalize_price(symbol, two_r_tp)
                                    existing_tp = pos.get('tp', 0.0)
                                    set_2r = False
                                    if existing_tp == 0.0:
                                        set_2r = True
                                    elif is_buy and two_r_tp < existing_tp:
                                        set_2r = True   
                                    elif not is_buy and two_r_tp > existing_tp:
                                        set_2r = True
                                    if set_2r and "XAU" not in symbol and "XAG" not in symbol:
                                        tp_req = {
                                            "action":   mt5.TRADE_ACTION_SLTP,
                                            "position": ticket,
                                            "sl":       lock_price,
                                            "tp":       two_r_tp,
                                        }
                                        # [SPRINT 18a] MT5 Lock
                                        with self.mt5_lock:
                                            tp_res = mt5.order_send(tp_req)
                                        if tp_res and tp_res.retcode == mt5.TRADE_RETCODE_DONE:
                                            self.log_info(
                                                f"🎯 2R TP Set: {symbol} residual half locked at "
                                                f"{two_r_tp:.5f} (2R = {sl_dist_dynamic * 2:.5f} from entry)"
                                            )
                                except Exception as _p2e:
                                    self.log_debug(f"S15-P2 2R TP error ({symbol}): {_p2e}")
                    else:
                        self.scaled_positions.add(scale_key)
                        self._save_state()

                if magic == 510001:
                    nano_trigger = 0.030 if "JPY" in symbol else 0.00030 
                    if profit_dist > nano_trigger:
                        secured_dist = profit_dist * 0.80
                        lock_price = open_price + secured_dist if is_buy else open_price - secured_dist
                        
                else:
                    one_r  = sl_dist_dynamic
                    two_r  = sl_dist_dynamic * 2.0

                    tp_price = pos.get('tp', 0.0)
                    tp_dist  = abs(tp_price - open_price) if tp_price else 0.0
                    near_tp  = (tp_dist > 0 and profit_dist >= tp_dist * 0.50)

                    if near_tp:
                        tight_trail = sl_dist_dynamic * 0.30
                        lock_price = (price_current - tight_trail if is_buy
                                      else price_current + tight_trail)
                    elif profit_dist > two_r:
                        lock_price = open_price + (profit_dist * 0.80) if is_buy else open_price - (profit_dist * 0.80)
                    elif profit_dist > one_r:
                        lock_price = open_price + (profit_dist * 0.50) if is_buy else open_price - (profit_dist * 0.50)

                    if profit_dist >= two_r and not near_tp:
                        try:
                            r_step  = 1.5 if ("XAU" in symbol or "XAG" in symbol) else 1.0
                            current_r = profit_dist / sl_dist_dynamic
                            import math
                            last_r_floor = math.floor(current_r)
                            ratchet_tp = (price_current + sl_dist_dynamic * r_step if is_buy
                                          else price_current - sl_dist_dynamic * r_step)
                            ratchet_tp = self.gateway.normalize_price(symbol, ratchet_tp)
                            existing_tp = pos.get('tp', 0.0)
                            advance = False
                            if existing_tp == 0.0:
                                advance = True
                            elif is_buy and ratchet_tp < existing_tp:
                                advance = True
                            elif not is_buy and ratchet_tp > existing_tp:
                                advance = True
                            if advance:
                                ratchet_req = {
                                    "action":   mt5.TRADE_ACTION_SLTP,
                                    "position": ticket,
                                    "sl":       current_sl,
                                    "tp":       ratchet_tp,
                                }
                                # [SPRINT 18a] MT5 Lock
                                with self.mt5_lock:
                                    r_res = mt5.order_send(ratchet_req)
                                if r_res and r_res.retcode == mt5.TRADE_RETCODE_DONE:
                                    self.log_info(
                                        f"🎯 TP Ratcheted: {symbol} at {current_r:.1f}R → "
                                        f"new TP {ratchet_tp:.5f} (+{r_step}R ahead)"
                                    )
                        except Exception as _p3e:
                            self.log_debug(f"S15-P3 ratchet error ({symbol}): {_p3e}")
                
                if lock_price == 0: 
                    continue
                
                if is_buy:
                    max_allowed_sl = price_current - min_stop_dist
                    if lock_price > max_allowed_sl: 
                        lock_price = max_allowed_sl
                else:
                    min_allowed_sl = price_current + min_stop_dist
                    if lock_price < min_allowed_sl: 
                        lock_price = min_allowed_sl

                should_modify = False
                if current_sl == 0: 
                    should_modify = True
                elif is_buy and lock_price > current_sl: 
                    should_modify = True
                elif not is_buy and lock_price < current_sl: 
                    should_modify = True
                    
                if should_modify:
                    lock_price = self.gateway.normalize_price(symbol, lock_price) 
                    
                    req = {
                        "action": mt5.TRADE_ACTION_SLTP, 
                        "position": ticket,
                        "sl": lock_price, 
                        "tp": pos.get('tp', 0.0)
                    }
                    # [SPRINT 18a] MT5 Lock
                    with self.mt5_lock:
                        res = mt5.order_send(req)
                    if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                        self.log_info(f"🛡️ Dynamic Profit Locked: {symbol} SL secured at {lock_price}")
            except Exception:
                pass

    def process_symbol(self, symbol, is_sniper_mode=False, upcoming_news=None):
        now = datetime.utcnow()
        if upcoming_news is None: 
            upcoming_news = []

        in_news_window, news_reason = self.news_manager.is_news_window(now=now)
        if in_news_window:
            if now.second < 5:
                self.log_info(f"📰 News Guard Active: Blocking {symbol} — {news_reason}")
            return

        if symbol in self.active_tickets or symbol in self.execution_lock: 
            return 

        props = self.gateway.get_symbol_properties(symbol)
        if not props: 
            return
        
        spread = (props['ask'] - props['bid']) / props['point']
        
        if "BTC" in symbol or "ETH" in symbol:
            limit = 50000
        elif "US SP 500" in symbol or "US Tech 100" in symbol or "Germany" in symbol:
            limit = 5000
        elif "XAU" in symbol or "XAG" in symbol or "Oil" in symbol or "NGAS" in symbol:
            limit = 1000
        else:
            limit = 60
            
        if spread > limit: 
            return 
        
        pending_orders = mt5.orders_get(symbol=symbol)
        if pending_orders and len(pending_orders) > 0:
            return 

        df_micro = self.gateway.get_market_data(symbol, timeframe=mt5.TIMEFRAME_M15)
        df_macro = self.gateway.get_market_data(symbol, timeframe=mt5.TIMEFRAME_H4)
        
        if df_micro.empty or df_macro.empty: 
            return

        symbol_regime = self.get_asset_regime(symbol)

        try:
            candles_micro = [Candle(**row) for row in df_micro.to_dict('records') if hasattr(row['time'], 'year')]
            req = AnalysisRequest(symbol=symbol, candles=candles_micro, daily_trend="NEUTRAL")
            
            analysis = analyze_market_structure(
                req, df_macro=df_macro, market_regime=symbol_regime, symbol=symbol
            )
            
            result_status = "SKIPPED"
            _is_micro_sig = "MICRO" in analysis.signal
            if is_sniper_mode:
                required_conf = 0.75 if _is_micro_sig else 0.90
            else:
                required_conf = 0.65 if _is_micro_sig else 0.80

            if analysis.signal != "NEUTRAL":
                 is_nano = "NANO" in analysis.signal
                 if is_nano and any(x in symbol for x in ["XAU", "XAG", "Oil", "NGAS", "BTC", "ETH", "US SP 500", "US Tech 100", "Germany"]):
                     self.log_debug(f"[{symbol}] NANO LOCK: Skipped (Spread drag too high).")
                     return

                 if analysis.confidence >= required_conf:
                     if is_sniper_mode:
                         self.log_info(f"🎯 GLOBAL SNIPER OVERRIDE: {symbol} {analysis.signal} (Conf: {analysis.confidence*100:.0f}%)")
                     else:
                         self.log_info(f"🔎 MTF Confluence Locked: {symbol} {analysis.signal} (Conf: {analysis.confidence*100:.0f}%)")
                     
                     result_status = "ATTEMPTED"
                     self.execute_signal(symbol, analysis, df_micro, props, regime=symbol_regime)
                 else:
                     result_status = f"LOW_CONFIDENCE ({analysis.confidence*100:.0f}%)"
                     _reason_str = analysis.reason or ""
                     if "NY Lunch" in _reason_str or "Reaccumulation" in _reason_str:
                         _last = getattr(self, '_ny_lunch_last_log', {})
                         _now  = datetime.utcnow()
                         if (_now - _last.get(symbol, datetime(2000,1,1))).total_seconds() > 900:
                             self.log_debug(f"[{symbol}] {_reason_str}")
                             _last[symbol] = _now
                             self._ny_lunch_last_log = _last
                     else:
                         self.log_debug(f"[{symbol}] {_reason_str}")
            else:
                 _reason_str = analysis.reason or ""
                 if "NY Lunch" in _reason_str or "Reaccumulation" in _reason_str:
                     _last = getattr(self, '_ny_lunch_last_log', {})
                     _now  = datetime.utcnow()
                     if (_now - _last.get(symbol, datetime(2000,1,1))).total_seconds() > 900:
                         self.log_debug(f"[{symbol}] {_reason_str}")
                         _last[symbol] = _now
                         self._ny_lunch_last_log = _last
                 else:
                     self.log_debug(f"[{symbol}] {_reason_str}")
                 
            conf_bucket = round(analysis.confidence, 2)
            last        = self._last_logged_signal.get(symbol)
            should_log  = (
                result_status == "ATTEMPTED"                             
                or last is None                                          
                or last[0] != analysis.signal                            
                or abs(last[1] - conf_bucket) >= 0.02                    
                or (result_status != "SKIPPED" and last[2] == "SKIPPED") 
            )

            if should_log:
                safe_reason    = getattr(analysis, 'reason', 'No reason provided')
                ict_conditions = getattr(analysis, 'ict_conditions', None)
                kill_zone      = getattr(analysis, 'kill_zone', None)
                ict_score      = getattr(analysis, 'ict_score', None)
                indicators     = {"trend": "MTF_Managed", "reason": safe_reason,
                                  "regime": symbol_regime}
                DBManager.log_signal(symbol, analysis.signal, analysis.confidence,
                                     indicators, result_status,
                                     ict_score=ict_score,
                                     kill_zone=kill_zone,
                                     ict_conditions=ict_conditions,
                                     model_type="ICT_STANDARD",
                                     model_sizing="STANDARD",
                                     account_id=self._get_current_account_id())
                self._last_logged_signal[symbol] = (analysis.signal, conf_bucket, result_status)

        except Exception as e: 
            self.log_debug(f"Process Error on {symbol}: {e}")

    def execute_signal(self, symbol, analysis, df, props, regime="NORMAL"):
        if symbol in self.execution_lock: 
            return
        
        self.execution_lock.add(symbol)
        
        def _async_execute():
            try:
                is_nano = "NANO" in analysis.signal
                is_buy = "BUY" in analysis.signal
                
                tick = mt5.symbol_info_tick(symbol)
                if not tick: 
                    return
                    
                local_high = df.tail(15)['high'].max()
                local_low = df.tail(15)['low'].min()
                structure_range = local_high - local_low
                
                min_buffer = 10.0 if "BTC" in symbol else 2.0 if "ETH" in symbol else 0.50 if ("XAU" in symbol or "Oil" in symbol or "NGAS" in symbol) else 0.10 if "JPY" in symbol else 0.0010
                volatility_buffer = max(structure_range, min_buffer)

                if "XAU" in symbol or "XAG" in symbol:
                    atr_val = df['atr'].iloc[-1] if 'atr' in df.columns else volatility_buffer
                    volatility_buffer = max(atr_val * 0.5, volatility_buffer)

                magic_number = 510001 if is_nano else 510000

                base_nano_sl = volatility_buffer * 0.4
                floor_sl = 0.060 if "JPY" in symbol else 0.00060  
                ceil_sl = 0.150 if "JPY" in symbol else 0.00150   
                
                dynamic_nano_sl = max(floor_sl, min(base_nano_sl, ceil_sl))
                dynamic_nano_tp = dynamic_nano_sl * 2.0

                ict_cond     = getattr(analysis, 'ict_conditions', {}) or {}
                ob_entry     = ict_cond.get('ob_entry_price')    
                ob_zone_low  = ict_cond.get('ob_zone_low')       
                ob_zone_high = ict_cond.get('ob_zone_high')      
                swing_sl_ref = ict_cond.get('swing_sl_ref')      
                sl_atr_buf   = ict_cond.get('sl_atr_buffer', volatility_buffer * 0.1)
                tp_target    = ict_cond.get('tp_target_level')   

                is_scalp_model = ict_cond.get('scalp_model', False)
                if is_scalp_model:
                    tfvg_high  = ict_cond.get('tfvg_high')
                    tfvg_low   = ict_cond.get('tfvg_low')
                    tfvg_mid   = ict_cond.get('tfvg_mid')
                    tfvg_size  = (tfvg_high - tfvg_low) if (tfvg_high and tfvg_low) else 0
                    asian_high = ict_cond.get('asian_high')
                    asian_low  = ict_cond.get('asian_low')
                    is_btc_eth = any(x in symbol for x in ('BTC', 'ETH'))

                    if tfvg_mid and is_buy:
                        ob_entry    = tfvg_mid
                        ob_zone_low = tfvg_low
                        if tfvg_low:
                            swing_sl_ref = tfvg_low
                            sl_atr_buf   = volatility_buffer * 0.15
                        if asian_high:
                            ext = volatility_buffer * 0.30 if 'XAU' in symbol else 0.0
                            ext = volatility_buffer * 0.20 if is_btc_eth else ext
                            tp_target = asian_high + ext
                        elif tfvg_size > 0:
                            mult = 1.5 if is_btc_eth else 2.0
                            tp_target = tfvg_mid + tfvg_size * mult
                        self.log_info(
                            f"📐 Scalp Levels [{symbol} BUY]: "
                            f"Entry≈{ob_entry:.5f}  SL≈{swing_sl_ref:.5f}  "
                            f"TP≈{tp_target:.5f}  (FVG mid={tfvg_mid:.5f} "
                            f"Asian_H={asian_high})"
                        )

                    elif tfvg_mid and not is_buy:
                        ob_entry     = tfvg_mid
                        ob_zone_high = tfvg_high
                        if tfvg_high:
                            swing_sl_ref = tfvg_high
                            sl_atr_buf   = volatility_buffer * 0.15
                        if asian_low:
                            ext = volatility_buffer * 0.30 if 'XAU' in symbol else 0.0
                            ext = volatility_buffer * 0.20 if is_btc_eth else ext
                            tp_target = asian_low - ext
                        elif tfvg_size > 0:
                            mult = 1.5 if is_btc_eth else 2.0
                            tp_target = tfvg_mid - tfvg_size * mult
                        self.log_info(
                            f"📐 Scalp Levels [{symbol} SELL]: "
                            f"Entry≈{ob_entry:.5f}  SL≈{swing_sl_ref:.5f}  "
                            f"TP≈{tp_target:.5f}  (FVG mid={tfvg_mid:.5f} "
                            f"Asian_L={asian_low})"
                        )

                if is_buy:
                    if is_nano:
                        action = "BUY_MARKET"
                        raw_price = tick.ask
                        sl_price = tick.bid - dynamic_nano_sl
                        tp_price = tick.ask + dynamic_nano_tp
                    else:
                        action = "BUY_LIMIT"
                        raw_price = ob_entry or ob_zone_low or df.iloc[-2]['low']

                        if swing_sl_ref is not None:
                            sl_price = swing_sl_ref - sl_atr_buf
                        else:
                            sl_price = df.iloc[-3]['low'] - (volatility_buffer * 0.1)

                        _buy_min_sl_guard = (100.0  if "BTC"     in symbol else
                                             5.0    if "ETH"     in symbol else
                                             1.5    if "XAU"     in symbol else
                                             0.10   if "XAG"     in symbol else
                                             0.50   if "Oil"     in symbol else  
                                             0.05   if "NGAS"    in symbol else
                                             1.0    if "SP 500"  in symbol else
                                             2.0    if ("Tech 100" in symbol or "Germany" in symbol) else
                                             0.10   if "JPY"     in symbol else
                                             0.0005)
                        _min_sl_from_entry_buy = raw_price - (_buy_min_sl_guard * 1.20)
                        if sl_price > _min_sl_from_entry_buy:
                            sl_price = _min_sl_from_entry_buy

                        if tp_target:
                            tp_price = tp_target
                        else:
                            tp_price = df.tail(30)['high'].max() + (volatility_buffer * 0.1)
                else:
                    if is_nano:
                        action = "SELL_MARKET"
                        raw_price = tick.bid
                        sl_price = tick.ask + dynamic_nano_sl
                        tp_price = tick.bid - dynamic_nano_tp
                    else:
                        action = "SELL_LIMIT"
                        raw_price = ob_entry or ob_zone_high or df.iloc[-2]['high']

                        if swing_sl_ref is not None:
                            sl_price = swing_sl_ref + sl_atr_buf
                        else:
                            sl_price = df.iloc[-3]['high'] + (volatility_buffer * 0.1)

                        _sell_min_sl_guard = (100.0  if "BTC"     in symbol else
                                              5.0    if "ETH"     in symbol else
                                              1.5    if "XAU"     in symbol else
                                              0.10   if "XAG"     in symbol else
                                              0.50   if "Oil"     in symbol else  
                                              0.05   if "NGAS"    in symbol else
                                              1.0    if "SP 500"  in symbol else
                                              2.0    if ("Tech 100" in symbol or "Germany" in symbol) else
                                              0.10   if "JPY"     in symbol else
                                              0.0005)
                        _min_sl_from_entry_sell = raw_price + (_sell_min_sl_guard * 1.20)
                        if sl_price < _min_sl_from_entry_sell:
                            sl_price = _min_sl_from_entry_sell

                        if tp_target:
                            tp_price = tp_target
                        else:
                            tp_price = df.tail(30)['low'].min() - (volatility_buffer * 0.1)
                    
                price = self.gateway.normalize_price(symbol, raw_price)
                sl = self.gateway.normalize_price(symbol, sl_price) 

                sl_dist_check = abs(price - sl_price) if sl_price else 0
                if sl_dist_check > 0 and tp_price:
                    tp_dist_check = abs(tp_price - price)
                    tp_r          = tp_dist_check / sl_dist_check
                    if tp_r > 6.0 and "XAU" not in symbol and "XAG" not in symbol:
                        capped_tp    = price + (sl_dist_check * 3.0) if is_buy else price - (sl_dist_check * 3.0)
                        tp_price     = capped_tp
                        self.log_info(
                            f"🎯 TP Cap Applied: {symbol} structural TP was {tp_r:.1f}R "
                            f"→ capped at 3.0R ({tp_price:.5f})"
                        )

                tp = self.gateway.normalize_price(symbol, tp_price)

                if not is_nano:
                    try:
                        _s17_atr = df['atr'].iloc[-1] if 'atr' in df.columns else volatility_buffer
                        _s17_dir = 'BUY' if is_buy else 'SELL'
                        _s17_chk = detect_candlestick_pattern(df, _s17_dir, _s17_atr)
                        _hard_block_patterns = {
                            'Bearish_Engulfing', 'Bullish_Engulfing',
                            'Shooting_Star', 'Hammer',
                        }
                        if (_s17_chk.get('conflict') and
                                _s17_chk.get('conflict_pattern') in _hard_block_patterns):
                            self.log_info(
                                f"🕯️ Candle Conflict [{symbol}]: "
                                f"{_s17_chk['conflict_pattern']} opposes "
                                f"{analysis.signal} at entry {price:.5f} — "
                                f"LIMIT order suppressed."
                            )
                            return
                    except Exception:
                        pass   

                if not is_nano:
                    sym_info = mt5.symbol_info(self.gateway.find_symbol(symbol) or symbol)
                    if sym_info:
                        stops_pt = getattr(sym_info, 'stops_level', 0) * sym_info.point
                        min_dist = stops_pt + (sym_info.point * 2)
                        
                        _idx_syms = {"US SP 500", "US Tech 100", "Germany 40"}
                        _crypto_syms = {"BTCUSD", "ETHUSD"}
                        if symbol in _idx_syms:
                            _rnd = max(1, round(price / 5))  
                            rejection_key = f"{symbol}_{_rnd}"
                        elif symbol in _crypto_syms:
                            rejection_key = f"{symbol}_{round(price, 0)}"
                        elif symbol in ("XAUUSD",):
                            rejection_key = f"{symbol}_{round(price, 1)}"
                        else:
                            rejection_key = f"{symbol}_{round(price, 4)}"
                            
                        stale_zone_key = (symbol, rejection_key)
                        if stale_zone_key in self._stale_zone_cooldowns:
                            elapsed = (datetime.utcnow() - self._stale_zone_cooldowns[stale_zone_key]).total_seconds()
                            if elapsed < 900:  
                                return  
                            else:
                                self._stale_zone_cooldowns.pop(stale_zone_key, None)
                                
                        if is_buy and price >= (tick.bid - min_dist):
                            self._price_close_rejections[rejection_key] = \
                                self._price_close_rejections.get(rejection_key, 0) + 1
                            count = self._price_close_rejections[rejection_key]
                            self.log_info(
                                f"⚠️ Price Validation: {symbol} BUY_LIMIT {price} too close "
                                f"to market ({tick.bid}). Min distance: {min_dist:.5f}. "
                                f"Rejection #{count}/4."
                            )
                            if count >= 4:
                                self._price_close_rejections.pop(rejection_key, None)
                                self._stale_zone_cooldowns[stale_zone_key] = datetime.utcnow()  
                                self.log_info(
                                    f"🚫 Stale Zone Invalidated: {symbol} entry {price} "
                                    f"rejected 4× — OB swept. Signal cancelled. 15-min cooldown started."
                                )
                                DBManager.update_signal_result(
                                    symbol, analysis.signal, "STALE_ZONE (4× rejected)"
                                )
                            return
                        elif not is_buy and price <= (tick.ask + min_dist):
                            self._price_close_rejections[rejection_key] = \
                                self._price_close_rejections.get(rejection_key, 0) + 1
                            count = self._price_close_rejections[rejection_key]
                            self.log_info(
                                f"⚠️ Price Validation: {symbol} SELL_LIMIT {price} too close "
                                f"to market ({tick.ask}). Min distance: {min_dist:.5f}. "
                                f"Rejection #{count}/4."
                            )
                            if count >= 4:
                                self._price_close_rejections.pop(rejection_key, None)
                                self._stale_zone_cooldowns[stale_zone_key] = datetime.utcnow()  
                                self.log_info(
                                    f"🚫 Stale Zone Invalidated: {symbol} entry {price} "
                                    f"rejected 4× — OB swept. Signal cancelled. 15-min cooldown started."
                                )
                                DBManager.update_signal_result(
                                    symbol, analysis.signal, "STALE_ZONE (4× rejected)"
                                )
                            return
                        else:
                            self._price_close_rejections.pop(rejection_key, None)
                            self._stale_zone_cooldowns.pop(stale_zone_key, None)  

                sl_distance = abs(price - sl)

                if not is_nano:
                    try:
                        _sym_info_41 = mt5.symbol_info(self.gateway.find_symbol(symbol) or symbol)
                        if _sym_info_41:
                            _stops_pt = getattr(_sym_info_41, 'stops_level', 0) * _sym_info_41.point
                            _min_stop_dist = _stops_pt + (_sym_info_41.point * 3)   
                            if is_buy:
                                _sl_to_current = abs(tick.bid - sl)
                            else:
                                _sl_to_current = abs(tick.ask - sl)
                            if _sl_to_current < _min_stop_dist and _min_stop_dist > 0:
                                if is_buy:
                                    sl = self.gateway.normalize_price(
                                        symbol, tick.bid - _min_stop_dist)
                                else:
                                    sl = self.gateway.normalize_price(
                                        symbol, tick.ask + _min_stop_dist)
                                sl_distance = abs(price - sl)
                                self.log_info(
                                    f"ℹ️ SL expanded [{symbol}]: current-price stops_level "
                                    f"check. SL adjusted to {sl:.5f} (dist {_sl_to_current:.5f} "
                                    f"→ {_min_stop_dist:.5f})"
                                )
                    except Exception:
                        pass   

                if not is_nano:
                    if "BTC" in symbol:
                        min_sl_guard = 100.0      
                    elif "ETH" in symbol:
                        min_sl_guard = 5.0
                    elif "XAU" in symbol:
                        min_sl_guard = 1.5        
                    elif "XAG" in symbol:
                        min_sl_guard = 0.10
                    elif "Oil" in symbol:
                        min_sl_guard = 0.50
                    elif "NGAS" in symbol:
                        min_sl_guard = 0.01
                    elif "SP 500" in symbol:
                        min_sl_guard = 1.0
                    elif "Tech 100" in symbol or "Germany" in symbol:
                        min_sl_guard = 2.0
                    elif "JPY" in symbol:
                        min_sl_guard = 0.10       
                    else:
                        min_sl_guard = 0.0005     
                    if sl_distance < min_sl_guard:
                        self.log_info(
                            f"⚠️ SL Guard: {symbol} SL distance {sl_distance:.5f} "
                            f"below minimum {min_sl_guard}. Skipping."
                        )
                        return

                acc_info = self.gateway.get_account_info()
                balance = acc_info['balance'] if acc_info else 10000.0
                free_margin = acc_info['free_margin'] if acc_info else 10000.0
                margin_level = acc_info.get('margin_level', 0.0)
                
                if margin_level > 0.0 and margin_level < 300.0:
                    self.log_info(f"🛡️ Margin Armor Active: Cannot open {symbol}. Margin Level critically low ({margin_level:.2f}%)")
                    return
                
                if free_margin < (balance * 0.15):
                    self.log_info(f"⚠️ Margin Alert: Cannot open {symbol}. Free Margin too low (${free_margin:.2f})")
                    return
                
                quant_params    = self.quant_engine.get_live_risk_params()
                kelly_risk      = quant_params.get('risk_pct', 0.02)
                regime_mult_q   = quant_params.get('regime_multiplier', 1.0)

                margin_mult     = 0.5 if (margin_level > 0.0 and margin_level < 500.0) else 1.0
                reduction_mult  = 0.5 if self._risk_reduction_mode else 1.0

                conf_scale      = (analysis.confidence - 0.80) / (0.99 - 0.80)
                conf_scale      = max(0.0, min(1.0, conf_scale))
                base_risk_pct   = kelly_risk * (1.0 + 0.25 * conf_scale)

                if analysis.confidence >= 0.92 and not is_nano:
                    base_risk_pct *= 1.25

                is_micro = "MICRO" in analysis.signal
                risk_multiplier = margin_mult * reduction_mult * regime_mult_q
                if is_nano:
                    risk_multiplier = risk_multiplier * 0.25   
                elif is_micro:
                    risk_multiplier = risk_multiplier * 0.50   

                if "XAU" in symbol or "XAG" in symbol:
                    risk_capital    = (balance * base_risk_pct) * risk_multiplier
                    capital_per_lot = sl_distance * 100
                    min_lot  = 0.10
                    vol_step = 0.01
                elif "BTC" in symbol:
                    risk_capital    = (balance * base_risk_pct * 0.5) * risk_multiplier
                    capital_per_lot = sl_distance * 1
                    min_lot  = 0.01
                    vol_step = 0.01
                elif "ETH" in symbol:
                    risk_capital    = (balance * base_risk_pct * 0.5) * risk_multiplier
                    capital_per_lot = sl_distance * 1
                    min_lot  = 0.01
                    vol_step = 0.01
                elif "Oil" in symbol:
                    risk_capital    = (balance * base_risk_pct * 0.5) * risk_multiplier
                    capital_per_lot = sl_distance * 100.0  
                    min_lot  = 1.0     
                    vol_step = 1.0     
                elif "NGAS" in symbol:
                    risk_capital    = (balance * base_risk_pct * 0.5) * risk_multiplier
                    capital_per_lot = sl_distance * 10000.0  
                    min_lot  = 0.1
                    vol_step = 0.1
                elif "SP 500" in symbol:
                    risk_capital    = (balance * base_risk_pct) * risk_multiplier
                    capital_per_lot = sl_distance * 1.0
                    min_lot  = 0.1
                    vol_step = 0.1
                elif "Tech 100" in symbol:
                    risk_capital    = (balance * base_risk_pct) * risk_multiplier
                    capital_per_lot = sl_distance * 1.0
                    min_lot  = 0.1
                    vol_step = 0.1
                elif "Germany" in symbol:
                    risk_capital    = (balance * base_risk_pct) * risk_multiplier
                    capital_per_lot = sl_distance * 1.0
                    min_lot  = 0.1
                    vol_step = 0.1
                elif "JPY" in symbol:
                    risk_capital    = (balance * base_risk_pct) * risk_multiplier
                    capital_per_lot = sl_distance * 1000
                    min_lot  = 0.30
                    vol_step = 0.01
                else:
                    risk_capital    = (balance * base_risk_pct) * risk_multiplier
                    capital_per_lot = sl_distance * 100000
                    min_lot  = 0.30
                    vol_step = 0.01

                raw_lot        = risk_capital / capital_per_lot
                import math as _math
                step_inv       = round(1.0 / vol_step)
                calculated_lot = _math.floor(raw_lot * step_inv) / step_inv
                lot = max(min_lot, calculated_lot)

                if "BTC" in symbol or "ETH" in symbol:
                    max_lot = max(0.5, round(balance / 20000, 2))
                elif "XAU" in symbol or "XAG" in symbol:
                    max_lot = 3.0 if "XAU" in symbol else 2.5
                elif "Oil" in symbol:
                    max_lot = max(1.0, min(10, int(round(balance / 1500, 0))))   
                elif "NGAS" in symbol:
                    max_lot = 5.0    
                elif "SP 500" in symbol:
                    max_lot = max(0.1, min(30, round(balance / 500, 1)))   
                elif "Tech 100" in symbol:
                    max_lot = max(0.1, min(20, round(balance / 500, 1)))   
                elif "Germany" in symbol:
                    max_lot = max(0.1, min(20, round(balance / 500, 1)))   
                elif is_nano:
                    max_lot = 0.10
                elif is_micro:
                    max_lot = max(round(balance / 12000, 2), 0.30)   
                else:
                    max_lot = max(1.0, round(balance / 3500, 2))    

                if is_nano:
                    max_lot = min(max_lot, 0.10)
                elif is_micro:
                    micro_cap = max(round(balance / 12000, 2), 0.30)
                    max_lot   = min(max_lot, max(micro_cap, min_lot))  

                if lot > max_lot:
                    capped = _math.floor(max_lot * step_inv) / step_inv
                    capped = max(capped, min_lot)
                    self.log_info(
                        f"⚠️ Lot Cap: {symbol} calculated {lot} lots → capped at {capped} "
                        f"({'MICRO' if is_micro else 'NANO' if is_nano else 'STANDARD'})"
                    )
                    lot = capped
                
                filling_mode_code = props.get('filling_mode', 0)
                if filling_mode_code & 1:
                    type_filling = mt5.ORDER_FILLING_FOK
                elif filling_mode_code & 2:
                    type_filling = mt5.ORDER_FILLING_IOC
                else:
                    type_filling = mt5.ORDER_FILLING_RETURN 

                try:
                    _fresh_tick = mt5.symbol_info_tick(self.gateway.find_symbol(symbol) or symbol)
                    _sinfo = mt5.symbol_info(self.gateway.find_symbol(symbol) or symbol)
                    if _sinfo and _fresh_tick:
                        _digits = getattr(_sinfo, 'digits', 5)
                        price = round(price, _digits)
                        sl    = round(sl,    _digits)
                        tp    = round(tp,    _digits) if tp else tp
                        if not is_nano:
                            if is_buy and price >= _fresh_tick.ask:
                                self.log_info(f"⏭️ Skip {symbol} BUY_LIMIT {price}: price above market ask {_fresh_tick.ask:.{_digits}f}. Order would be filled immediately or rejected.")
                                return
                            if not is_buy and price <= _fresh_tick.bid:
                                self.log_info(f"⏭️ Skip {symbol} SELL_LIMIT {price}: price below market bid {_fresh_tick.bid:.{_digits}f}. Order would be filled immediately or rejected.")
                                return
                except Exception:
                    pass  

                request = {
                    "action": mt5.TRADE_ACTION_DEAL if is_nano else mt5.TRADE_ACTION_PENDING,
                    "symbol": symbol,
                    "volume": float(lot),
                    "price": float(price),
                    "sl": float(sl),
                    "tp": float(tp),
                    "deviation": 10,
                    "magic": magic_number,
                    "comment": "Kom_v1.0",
                    "type_time": mt5.ORDER_TIME_GTC if is_nano else mt5.ORDER_TIME_SPECIFIED,
                    "type_filling": type_filling,
                }

                if not is_nano: 
                    request["expiration"] = int(time.time()) + (4 * 3600)
                
                if is_buy: 
                    request["type"] = mt5.ORDER_TYPE_BUY if is_nano else mt5.ORDER_TYPE_BUY_LIMIT
                else: 
                    request["type"] = mt5.ORDER_TYPE_SELL if is_nano else mt5.ORDER_TYPE_SELL_LIMIT

                fill_order   = [mt5.ORDER_FILLING_FOK,
                                mt5.ORDER_FILLING_IOC,
                                mt5.ORDER_FILLING_RETURN]
                fill_names   = ["FOK", "IOC", "RETURN"]
                FILL_ERR     = 10038   

                result = None
                if is_nano:
                    for idx, fmode in enumerate(fill_order):
                        request["type_filling"] = fmode
                        # [SPRINT 18a] MT5 Lock applied to execution
                        with self.mt5_lock:
                            result = mt5.order_send(request)
                        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                            break   
                        if result and result.retcode != FILL_ERR:
                            break   
                else:
                    # [SPRINT 18a] MT5 Lock applied to execution
                    with self.mt5_lock:
                        result = mt5.order_send(request)

                safe_action = action.replace("_", " ")

                if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                    fill_label = f" | Fill: {fill_names[fill_order.index(request['type_filling'])]}" if is_nano else ""
                    self.log_info(f"⚡ {'MARKET EXECUTION' if is_nano else 'TRAP SET'}: {symbol} {action} | Entry: {price} | Lot: {lot}{fill_label}")
                    self.async_alert(f"⚡ **SMC {safe_action}**: {symbol}\nTarget Entry: {price}\nLot: {lot}\nConf: {analysis.confidence*100:.0f}%")
                    DBManager.update_signal_result(symbol, analysis.signal, "FILLED")

                    acc_id = self._get_current_account_id()
                    if is_nano:
                        DBManager.save_trade(
                            ticket      = result.order,
                            symbol      = symbol,
                            type_op     = 'BUY' if is_buy else 'SELL',
                            vol         = float(lot),
                            open_price  = float(price),
                            sl          = float(sl),
                            tp          = float(tp),
                            time        = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
                            regime      = regime,
                            account_id  = acc_id,
                            model_type  = 'ICT_STANDARD',
                            model_sizing= 'STANDARD',
                        )
                    else:
                        self._pending_order_info[result.order] = {
                            'regime':       regime,
                            'account_id':   acc_id,
                            'model_type':   'ICT_STANDARD',
                            'model_sizing': 'STANDARD',
                        }
                else:
                    err_msg = result.comment if result else "Unknown MT5 Error"
                    retcode = result.retcode if result else -1
                    self.log_info(f"❌ MT5 REJECTED {symbol}: {err_msg} (retcode={retcode})")
                    self.symbol_cooldowns[symbol] = datetime.utcnow()
                    DBManager.update_signal_result(symbol, analysis.signal, f"REJECTED: {err_msg}")

            except Exception as e:
                self.log_info(f"⚠️ Thread Execution Error on {symbol}: {e}")
                self.symbol_cooldowns[symbol] = datetime.utcnow()
            finally:
                self.execution_lock.discard(symbol)
                
        threading.Thread(target=_async_execute).start()

# ==========================================
    # FULLY RESTORED DASHBOARD API TELEMETRY
    # ==========================================
    def get_status(self):
        acc = self.gateway.get_account_info()
        raw_pos = self.gateway.get_open_positions()
        return {
            "is_running": self.is_running,
            "active_users": 1,
            "watched_symbols": self.active_symbols,
            "recent_logs": self.logs,
            "account": acc,
            "positions": raw_pos,
            "total_pnl": sum(p['profit'] for p in raw_pos) if raw_pos else 0.0,
            "market_regime": self.market_regime,
            "daily_var": self.current_var,
            "kill_switch": self.kill_switch_active
        }        
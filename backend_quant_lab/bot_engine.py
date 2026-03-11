import os
import logging
import json
import pandas as pd
from datetime import datetime, timedelta
import time
import MetaTrader5 as mt5 
from mt5_interface import MT5Gateway
from analyst import analyze_market_structure, AnalysisRequest, calculate_atr
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
        
        # ==========================================
        # FULLY DIVERSIFIED ASSET MATRIX (target: 20 assets)
        # ==========================================
        # [SPRINT 8] 20-asset matrix: confirmed broker symbols.
        # US Oil = "US Oil" (Deriv), NGAS = Natural Gas, Germany 40 = DAX.
        # Routing checks use: 'Oil' in symbol, 'NGAS' in symbol, 'Germany' in symbol.
        self.vip_assets = [
            # Forex (11)
            "EURUSD", "GBPUSD", "USDJPY", "USDCAD", "USDCHF", "AUDUSD", "NZDUSD",
            "EURJPY", "GBPJPY", "EURGBP", "AUDJPY",
            # Metals (2)
            "XAUUSD", "XAGUSD",
            # Commodities (2) [SPRINT 8]
            "US Oil", "NGAS",
            # Crypto (2)
            "BTCUSD", "ETHUSD",
            # Indices (3) [SPRINT 8]
            "US SP 500", "US Tech 100", "Germany 40",
        ]
        
        self.active_symbols = [] 
        self.symbol_cooldowns = {} 
        
        # Expanded absolute capacity limits
        self.MAX_OPEN_TRADES = 12       
        self.MAX_SNIPER_SLOTS = 5      
        self.MAX_GOLD_TRADES = 3       # Reused as MAX_COMMODITY_TRADES (gold, silver, oil, ngas)       
        
        self.logs = []
        self.is_running = False
        self.active_tickets = set()
        self.execution_lock = set() 
        
        # --- STATE PERSISTENCE ---
        self.state_file = "logs/tradecore_state.json"
        self.scaled_positions = set()  # populated in start_service() once MT5 is connected
        
        self.daily_start_balance = 0.0
        self.last_trade_day = -1
        self.kill_switch_active = False
        self.kill_switch_time   = None    # [S9] tracks when KS last fired for 8h cooldown

        self.current_var = 0.0
        self.market_regime = "CALIBRATING..."

        # [OPT-6] Signal deduplication tracker.
        self._last_logged_signal: dict = {}

        # [SPRINT 9] Pending LIMIT fill tracker.
        # When a LIMIT order is placed successfully, we store its info here
        # keyed by MT5 order ticket.  run_cycle() detects when it fills
        # (appears in open positions) and calls save_trade() immediately.
        self._pending_order_info: dict = {}

        # [SPRINT 7] Quantitative analytics engine.
        # Cached for 5 min. Provides: risk_pct, var_limit, cvar_limit,
        # kelly_fraction, regime_gate, regime_multiplier.
        self.quant_engine = QuantEngine()
        # [SPRINT 7] Risk reduction flag — set by VaR warning, cleared when safe
        self._risk_reduction_mode = False

    def _get_current_account_id(self):
        """Safely fetch account ID from gateway — returns None if not connected yet."""
        try:
            return self.gateway.get_account_id()
        except Exception:
            return None

    def _load_state(self):
        """
        Load scaled_positions and _last_logged_signal from disk.
        If the saved account_id does not match the current MT5 account,
        the state is discarded and a clean set is returned.
        """
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
                # [S9] Restore signal dedup tracker — prevents first-cycle log flood
                raw_dedup = data.get("last_logged_signal", {})
                self._last_logged_signal = {k: tuple(v) for k, v in raw_dedup.items()}
                return set(data.get("scaled_positions", []))
        except Exception as e:
            self.log_debug(f"State Load Error: {e}")
        return set()

    def _save_state(self):
        """Save scaled_positions + signal dedup dict with current account_id."""
        try:
            account_id = self._get_current_account_id()
            # Serialize tuples as lists (JSON-safe)
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
        cmd = command.split()[0].lower()
        self.log_info(f"📩 Received Command: {cmd}")
        
        if cmd == "/status": 
            self._report_status()
        elif cmd == "/news":
            news_data = self.news_manager.get_upcoming_news()
            if not news_data: 
                self.async_alert("🌍 **No High Impact News Found.**")
            else:
                lines = ["📰 **Upcoming News Risks**"]
                for item in news_data[:5]:
                    icon = "🔴" if item['impact'] == 'High' else "🟠" 
                    lines.append(f"{icon} {item['time'].split()[-1]} • {item['country']} {item['title']}")
                self.async_alert("\n".join(lines))
        elif cmd == "/stop":
            self.stop_service()
            self.async_alert("🛑 **Bot Stopped by User Command**")
        elif cmd == "/start":
            if not self.is_running: 
                self.start_service()
        elif cmd == "/balance":
            acc = self.gateway.get_account_info()
            if acc: 
                self.async_alert(f"💰 **Balance:** ${acc['balance']:.2f}\n**Equity:** ${acc['equity']:.2f}")

    def _report_status(self):
        acc = self.gateway.get_account_info()
        if acc:
            balance = acc['balance']
            equity = acc['equity']
            free_margin = acc['free_margin']
            margin_level = acc.get('margin_level', 0.0)
        else:
            balance = equity = free_margin = margin_level = 0.0

        positions = self.gateway.get_open_positions()
        total_profit = sum(p['profit'] for p in positions)
        
        msg = (
            f"📊 **TradeCore v53.0 Status**\n"
            f"-------------------------\n"
            f"💰 Balance: ${balance:,.2f}\n"
            f"📈 Equity: ${equity:,.2f}\n"
            f"🛡️ Free Margin: ${free_margin:,.2f}\n"
            f"⚡ Margin Level: {margin_level:.2f}%\n"
            f"-------------------------\n"
            f"🎯 Active Trades: {len(positions)}\n"
            f"⏳ Pending Executions: {len(self.execution_lock)}\n"
            f"💵 Floating PnL: ${total_profit:,.2f}\n"
        )
        
        if positions:
            msg += "-------------------------\n"
            for p in positions:
                icon = "🟢" if p['profit'] >= 0 else "🔴"
                msg += f"{icon} {p['symbol']} ({p['type']}): ${p['profit']:.2f}\n"
                
        self.async_alert(msg)

    def send_daily_summary(self):
        """
        [S9] Daily Telegram summary — fires at 23:50 UTC via scheduler.
        Provides a full-day accountability report so the operator can review
        performance from their phone without accessing the terminal.
        Covers: balance, day P&L, weekly P&L target progress, signal funnel,
        upcoming news, kill-switch status.
        [S11] BUG-37 fixed: DB path corrected from "logs/tradecore.db" to "tradecore.db".
        [S11] Added weekly P&L tracker toward $3,000/week target.
        """
        try:
            acc = self.gateway.get_account_info()
            balance  = acc['balance']  if acc else 0.0
            equity   = acc['equity']   if acc else 0.0
            day_pnl  = balance - self.daily_start_balance

            positions = self.gateway.get_open_positions()
            floating  = sum(p['profit'] for p in positions)

            # Signal funnel from DB: today only
            # [BUG-37 FIX] DB lives at working directory root, NOT in logs/
            today_str = datetime.utcnow().strftime('%Y-%m-%d')
            try:
                import sqlite3 as _sl
                con = _sl.connect("tradecore.db")   # [BUG-37] was: "logs/tradecore.db"
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

            # [S11] Weekly P&L tracker toward $3,000/week target
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
            # Progress bar (10 segments)
            filled_bars = min(10, int(week_progress_pct / 10))
            bar = "█" * filled_bars + "░" * (10 - filled_bars)
            week_icon = "🎯" if weekly_pnl >= WEEKLY_TARGET else ("🟢" if weekly_pnl > 0 else "🔴")
            week_wr = f"{winners_this_week}/{trades_this_week}" if trades_this_week > 0 else "—"

            # Upcoming high-impact news (next 12 hours)
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
                f"📋 **TradeCore v53.0 — Daily Summary**\n"
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
        
        self.active_symbols = []
        for v in self.vip_assets:
            real = self.gateway.find_symbol(v)
            if real: 
                self.active_symbols.append(real)
                mt5.symbol_select(real, True)

        self.is_running = True
        self.execution_lock.clear()
        self.account_id = self._get_current_account_id()  # cache for this session
        # Load state NOW — MT5 is connected so account_id comparison works correctly.
        # Stale entries from a different account are discarded automatically here.
        self.scaled_positions = self._load_state()
        self.log_info(f"✅ TradeCore v53.0: Engine Active. Monitoring {len(self.active_symbols)} Assets.")
        
        self.news_manager.fetch_calendar()
        self.notifier.start_listening(self.handle_telegram_command)
        self.async_alert("🚀 **TradeCore v53.0 Master Online**\nDynamic Structural Targets Armed.")
        return True

    def stop_service(self):
        self.is_running = False
        self.notifier.stop_listening()
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
                res = mt5.order_send(req)
                if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                    self.log_info(f"🗑️ Stale Trap Avoided: Cancelled {symbol} limit order. ({reason})")

    def evaluate_open_positions(self, positions):
        for pos in positions:
            try:
                symbol = pos['symbol']
                is_buy = pos['type'] == 'BUY'
                
                duration_hours = (time.time() - pos['time']) / 3600.0
                profit = pos['profit']
                
                # RESTORED: Stable 12-Hour Time Decay. 
                # Prevents the AI from panicking and exiting valid setups early.
                # [SPRINT 7] MAE/MFE tick tracking — called every cycle
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
                    DBManager.update_mae_mfe(pos['ticket'], adverse, favorable)

                if duration_hours > 12.0 and profit < 0:
                    self.log_info(f"⏳ Time Decay Killswitch: {symbol} stuck in dead momentum for >12H. Liquidating.")
                    self.gateway.close_position(pos['ticket'], symbol, pos['volume'], pos['type'])
                    self.async_alert(f"⏳ **Dead Momentum Liquidated:** {symbol}\nTrade closed early to free up margin.")
                    self.symbol_cooldowns[symbol] = datetime.utcnow()  # [UTC FIX]
                    continue

            except Exception:
                pass

    def get_asset_regime(self, symbol: str) -> str:
        """
        [OPT-7] Per-asset-class regime detection.
        Old system used EURUSD for ALL assets — a ranging Friday EURUSD
        would flag BTC and Gold as DEAD MARKET even during strong moves.
        Each asset now measures its own 15-candle vol% for regime classification.
        Regime thresholds are calibrated per asset class.
        """
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

        # Thresholds calibrated per asset class
        if "BTC" in s or "ETH" in s:
            # Crypto: high vol = >3%, dead = <0.5%
            if vol_pct > 0.030:  return "HIGH VOLATILITY (GARCH)"
            if vol_pct > 0.005:  return "NORMAL (TRENDING)"
            return "DEAD MARKET"
        elif "XAU" in s or "XAG" in s:
            # Metals: high vol = >1%, dead = <0.15%
            if vol_pct > 0.010:  return "HIGH VOLATILITY (GARCH)"
            if vol_pct > 0.0015: return "NORMAL (TRENDING)"
            return "DEAD MARKET"
        elif "Oil" in s or "NGAS" in s:
            # Commodities (WTI Crude / Natural Gas): highly volatile
            # NGAS is more volatile than Oil — unified threshold is conservative
            if vol_pct > 0.020:  return "HIGH VOLATILITY (GARCH)"
            if vol_pct > 0.003:  return "NORMAL (TRENDING)"
            return "DEAD MARKET"
        elif "SP 500" in s or "Tech 100" in s or "Germany" in s:
            # Indices: high vol = >1.5%, dead = <0.2%
            if vol_pct > 0.015:  return "HIGH VOLATILITY (GARCH)"
            if vol_pct > 0.002:  return "NORMAL (TRENDING)"
            return "DEAD MARKET"
        elif "JPY" in s:
            # JPY crosses: high vol = >0.6%, dead = <0.08%
            if vol_pct > 0.006:  return "HIGH VOLATILITY (GARCH)"
            if vol_pct > 0.0008: return "NORMAL (TRENDING)"
            return "DEAD MARKET"
        else:
            # FX majors: high vol = >0.8%, dead = <0.1%
            if vol_pct > 0.008:  return "HIGH VOLATILITY (GARCH)"
            if vol_pct > 0.001:  return "NORMAL (TRENDING)"
            return "DEAD MARKET"

    def evaluate_risk_metrics(self, current_positions=None):
        """
        [OPT-5] Proper parametric VaR using ATR-based daily return std-dev.
        [SPRINT 9] Multi-asset VaR: uses max ATR% across all open positions,
        not EURUSD-only. A BTC or Gold spike now correctly elevates VaR.
        """
        try:
            import MetaTrader5 as mt5
            df = self.gateway.get_market_data("EURUSD")
            if df.empty or len(df) < 15:
                return "CALIBRATING...", 0.0

            recent_high   = df['high'].iloc[-15:].max()
            recent_low    = df['low'].iloc[-15:].min()
            current_price = df['close'].iloc[-1]
            vol_pct       = (recent_high - recent_low) / current_price

            if vol_pct > 0.008:    regime = "HIGH VOLATILITY (GARCH)"
            elif vol_pct > 0.003:  regime = "NORMAL (TRENDING)"
            else:                  regime = "DEAD MARKET"

            acc     = self.gateway.get_account_info()
            balance = acc['balance'] if acc else 10000.0

            df['atr']   = calculate_atr(df, period=14)
            atr_eurusd  = df['atr'].iloc[-1]
            base_std    = (atr_eurusd / current_price) if current_price > 0 and atr_eurusd > 0 else vol_pct

            # [S9] Multi-asset: compute ATR% for each open position symbol
            # and use the maximum as the portfolio vol driver. This means
            # a BTCUSD or XAUUSD spike correctly elevates the portfolio VaR
            # even if EURUSD is calm.
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

    def run_cycle(self):
        if not self.is_running: 
            return
        
        acc = self.gateway.get_account_info()
        if not acc: 
            return
        
        DBManager.log_snapshot(acc['balance'], acc['equity'], acc['margin_level'], acc['free_margin'],
                               account_id=acc.get('account_id'))
        current_positions = self.gateway.get_open_positions()

        # [S9] LIMIT FILL DETECTION ─────────────────────────────────────
        # When a LIMIT order fills, execute_signal stores its info in
        # _pending_order_info keyed by order ticket. Here we detect
        # when it appears in the open positions list and call save_trade()
        # immediately so model_type/account_id are properly recorded.
        if self._pending_order_info:
            db_open = DBManager.get_open_trade_tickets()
            for pos in current_positions:
                ticket = pos.get('ticket')
                if ticket in self._pending_order_info and ticket not in db_open:
                    info = self._pending_order_info.pop(ticket)
                    try:
                        open_time = datetime.utcfromtimestamp(
                            pos.get('time', 0)
                        ).strftime('%Y-%m-%d %H:%M:%S')
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
                        self.log_debug(f"[{pos['symbol']}] Trade recorded: "
                                       f"ticket={ticket} model={info.get('model_type')}")
                    except Exception as e:
                        self.log_debug(f"Fill Record Error ({ticket}): {e}")
        # ────────────────────────────────────────────────────────────────
        
        current_day = datetime.utcnow().day
        
        if self.kill_switch_active:
            now_utc         = datetime.utcnow()
            new_trading_day = (current_day != self.last_trade_day)
            hours_since_ks  = (
                (now_utc - self.kill_switch_time).total_seconds() / 3600.0
                if self.kill_switch_time else 99.0
            )
            # [S9] Require BOTH midnight crossing AND 8h elapsed since KS fired.
            # Prevents a 23:58 breach resetting at 00:00 (only 2 min later).
            if new_trading_day and hours_since_ks >= 8.0:
                self.log_info("🌅 Kill Switch Reset: New trading day + 8h cooldown passed.")
                self.kill_switch_active = False
                self.kill_switch_time   = None
                self.daily_start_balance = acc['balance']
                self.last_trade_day      = current_day
            else:
                if now_utc.minute % 30 == 0 and now_utc.second < 5:
                    hrs_remaining = max(0.0, 8.0 - hours_since_ks)
                    self.log_info(f"🛑 Kill Switch Active. "
                                  f"{hrs_remaining:.1f}h remaining before reset eligible.")
                return

        if current_day != self.last_trade_day:
            self.daily_start_balance = acc['balance']
            self.last_trade_day = current_day

        self.market_regime, self.current_var = self.evaluate_risk_metrics(current_positions)

        if self.daily_start_balance > 0 and self.current_var > 0:
            current_dd_usd = self.daily_start_balance - acc['equity']
            
            # [SPRINT 7] Two-stage kill switch: VaR warning → CVaR halt
            # CVaR (Expected Shortfall) fires first — it's the average loss
            # in the worst 1% of sessions. More conservative than VaR alone.
            quant_params = self.quant_engine.get_live_risk_params()
            cvar_limit   = quant_params.get('cvar_limit', self.current_var * 1.29)
            var_limit    = quant_params.get('var_limit',  self.current_var)

            if current_dd_usd >= cvar_limit:
                self.log_info(f"🛑 KILL SWITCH [CVaR]: Tail-risk limit breached! (DD: ${current_dd_usd:.2f} | CVaR: ${cvar_limit:.2f})")
                self.async_alert(f"🛑 **CRITICAL: CVaR BREACHED**\nExpected Shortfall limit hit (${cvar_limit:.2f}). Liquidating {len(current_positions)} positions.")
                self.close_all_positions(current_positions)
                self.kill_switch_active = True
                self.kill_switch_time   = datetime.utcnow()   # [S9] start 8h cooldown clock
                return
            elif current_dd_usd >= var_limit:
                self.log_info(f"⚠️ VaR WARNING: Drawdown ${current_dd_usd:.2f} hit VaR limit ${var_limit:.2f}. Halving position sizes.")
                self._risk_reduction_mode = True
            else:
                self._risk_reduction_mode = False

        # [SPRINT 7] Markov Regime Gate — check regime transition probabilities
        # If P(HIGH_VOL) > 50% or P(BEAR) > 65% → halt new positions
        markov = self.quant_engine.markov_regime()
        if markov.get('trading_gate') == 'HALT':
            if datetime.utcnow().second < 5:
                p_bear = markov.get('p_bear_next', 0)
                p_hv   = markov.get('p_high_vol_next', 0)
                self.log_info(f"🔴 Markov Gate HALT: P(BEAR)={p_bear:.0%} P(HIGH_VOL)={p_hv:.0%}. No new positions.")
            return
        elif markov.get('trading_gate') == 'REDUCE':
            self._risk_reduction_mode = True

        is_open, market_status = self.check_market_schedule()
        if not is_open:
            if datetime.now().minute % 30 == 0 and datetime.now().second < 5:
                self.log_info(f"💤 Market Offline: {market_status}. Bot standing by.")
            return 

        self.apply_trailing_stop(current_positions)
        self.evaluate_pending_orders() 
        self.evaluate_open_positions(current_positions) 
        self.active_tickets = {p['symbol'] for p in current_positions}

        gold_trades = len([p for p in current_positions if "XAU" in p['symbol'] or "XAG" in p['symbol']])
        current_count = len(current_positions) + len(self.execution_lock) 
        
        raw_orders = mt5.orders_get()
        pending_list = list(raw_orders) if raw_orders else []
        usd_exposure_base = len([p for p in current_positions if "USD" in p['symbol']]) + len([o for o in pending_list if "USD" in o.symbol])

        # [S9] BASE CURRENCY CORRELATION GUARD ──────────────────────────
        # Tracks how many open/pending positions share the same 3-char base.
        # Prevents silent EUR/GBP/AUD double-exposure (e.g. EURUSD + EURJPY).
        # The existing USD lock above handles USD-specific exposure.
        # Cap: max 2 positions with the same base currency simultaneously.
        base_exposure: dict = {}
        for p in current_positions:
            b = p['symbol'][:3]
            base_exposure[b] = base_exposure.get(b, 0) + 1
        for o in pending_list:
            b = o.symbol[:3]
            base_exposure[b] = base_exposure.get(b, 0) + 1
        # ────────────────────────────────────────────────────────────────

        if current_count >= (self.MAX_OPEN_TRADES + self.MAX_SNIPER_SLOTS):
            if datetime.now().second < 5: 
                self.log_info(f"⏸️ Absolute Capacity Full ({current_count}/{self.MAX_OPEN_TRADES + self.MAX_SNIPER_SLOTS}). System maxed out.")
            return

        is_sniper_mode = (current_count >= self.MAX_OPEN_TRADES)
        if is_sniper_mode and datetime.now().second < 5:
            self.log_info(f"🎯 Base capacity full. 92%+ Global Sniper Mode Active.")
        
        upcoming_news = self.news_manager.get_upcoming_news() if self.news_manager else []

        for symbol in self.active_symbols:
            if symbol in self.symbol_cooldowns:
                time_since_close = datetime.utcnow() - self.symbol_cooldowns[symbol]  # [UTC FIX]
                # [S9-FIX] Restore 15-min cooldown. 60-min was too aggressive:
                # a spread rejection at market open locked the symbol for a full hour.
                # 15 min prevents retry storms while keeping signal capture responsive.
                if time_since_close < timedelta(minutes=15):
                    continue 
            
            # Dynamic USD Exposure Lock
            current_usd_locks = len([s for s in self.execution_lock if "USD" in s])
            if "USD" in symbol and (usd_exposure_base + current_usd_locks) >= 2:
                continue

            # [S9] Base-currency correlation guard (EUR, GBP, AUD, NZD, etc.)
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

                # [S12-P1A] Dynamic 1:1 RR trigger — derived from the position's
                # actual SL distance, not a fixed pip value.
                # sl_distance = |open_price - sl|. Scale-out fires when
                # profit_dist ≥ sl_distance (true 1:1 RR), not at an arbitrary
                # 2-pip / 2pt threshold that may be well below 1R.
                # If SL is missing/zero (shouldn't happen post-fix but defensive),
                # fall back to the previous asset-class hard-coded values.
                sl_dist_dynamic = abs(open_price - current_sl) if current_sl and current_sl != 0.0 else 0.0

                # Fallback hard-coded floors (only used when SL data unavailable)
                if sl_dist_dynamic <= 0:
                    if   "XAU" in symbol or "XAG" in symbol or "Oil" in symbol or "NGAS" in symbol:
                        sl_dist_dynamic = 2.0
                    elif "BTC" in symbol or "ETH" in symbol or "US SP 500" in symbol or "US Tech 100" in symbol or "Germany" in symbol:
                        sl_dist_dynamic = 50.0
                    elif "JPY" in symbol:
                        sl_dist_dynamic = 0.200
                    else:
                        sl_dist_dynamic = 0.0050  # 5-pip floor for standard FX

                # [BUG-39 FIX] scale_key must NOT include the ticket.
                # After a partial close, MT5 can issue a new ticket to the residual
                # position. If the key included the ticket, scaled_positions would
                # miss the new key and scale-out would fire a second time.
                # symbol + open_price + type uniquely identifies a position intent
                # and is stable across partial fills and ticket reassignments.
                scale_key = f"{symbol}_{open_price}_{pos['type']}"
                is_ready_to_scale = profit_dist >= sl_dist_dynamic  # true 1:1 RR

                if is_ready_to_scale and scale_key not in self.scaled_positions:
                    half_vol = current_vol / 2.0
                    close_vol = round(half_vol / vol_step) * vol_step
                    
                    if close_vol >= min_lot:
                        self.log_info(f"⚖️ Scaling Out: {symbol} hit 1:1 RR. Closing {close_vol} Lots to secure cash.")
                        success = self.gateway.close_position(ticket, symbol, close_vol, pos['type'])
                        
                        if success:
                            self.scaled_positions.add(scale_key)
                            self._save_state()
                            self.async_alert(f"⚖️ **Partial Take Profit:** {symbol}\nSecured 50% Volume. Moving SL to Breakeven.")
                            breakeven_buffer = props['point'] * 5 
                            lock_price = open_price + breakeven_buffer if is_buy else open_price - breakeven_buffer
                    else:
                        self.scaled_positions.add(scale_key)
                        self._save_state()

                if magic == 510001:
                    nano_trigger = 0.030 if "JPY" in symbol else 0.00030 
                    if profit_dist > nano_trigger:
                        secured_dist = profit_dist * 0.80
                        lock_price = open_price + secured_dist if is_buy else open_price - secured_dist
                        
                else:
                    # [S12-P1A] Trail triggers also use 1×SL and 2×SL distance,
                    # not fixed pip values. This makes the trail proportional to
                    # the original risk: lock 50% profit at 1R, lock 80% at 2R.
                    one_r  = sl_dist_dynamic
                    two_r  = sl_dist_dynamic * 2.0
                    if profit_dist > two_r:
                        lock_price = open_price + (profit_dist * 0.80) if is_buy else open_price - (profit_dist * 0.80)
                    elif profit_dist > one_r:
                        lock_price = open_price + (profit_dist * 0.50) if is_buy else open_price - (profit_dist * 0.50)
                
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
                    res = mt5.order_send(req)
                    if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                        self.log_info(f"🛡️ Dynamic Profit Locked: {symbol} SL secured at {lock_price}")
            except Exception:
                pass

    def process_symbol(self, symbol, is_sniper_mode=False, upcoming_news=None):
        now = datetime.utcnow()   # [UTC FIX] was datetime.now() — standardized to utcnow()
        if upcoming_news is None: 
            upcoming_news = []

        # [BUG-25 FIX] Previous news guard used datetime.strptime() with a fixed
        # format string that never matched ForexFactory's actual date format, so
        # every parse raised ValueError → caught silently → news guard completely
        # disabled for all events. Replaced with news_manager.is_news_window()
        # which uses the pre-parsed _event_dt datetime objects directly.
        # Tier 1 events (NFP/FOMC/CPI) get 4h pre + 30min post block.
        # Tier 2 events get 15min each side.
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

        # [OPT-7] Per-asset-class regime: replaces the global EURUSD-only regime.
        # BTC/Gold/Indices now calibrate their own vol% against their own thresholds.
        symbol_regime = self.get_asset_regime(symbol)

        try:
            candles_micro = [Candle(**row) for row in df_micro.to_dict('records') if hasattr(row['time'], 'year')]
            req = AnalysisRequest(symbol=symbol, candles=candles_micro, daily_trend="NEUTRAL")
            
            # [OPT-7] Pass per-asset regime + symbol for calibrated ATR threshold
            analysis = analyze_market_structure(
                req, df_macro=df_macro, market_regime=symbol_regime, symbol=symbol
            )
            
            result_status = "SKIPPED"
            # [S9-PRECISION] Thresholds recalibrated for the new detection engine.
            # The precision upgrades (1.0 ATR displacement, swing-based sweep, 
            # in-zone OB retest, 1.5x volume, 100-bar PD) mean scores are earned
            # more rigorously. A 0.80 under the new engine is equivalent to or
            # better than 0.88 under the old loose detections.
            # Standard: 0.80 (requires sweep+disp+OB+PD at minimum)
            # Sniper:   0.90 (requires near-full confluence)
            required_conf = 0.90 if is_sniper_mode else 0.80

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
                     self.log_debug(f"[{symbol}] {analysis.reason}")
            else:
                 self.log_debug(f"[{symbol}] {analysis.reason}")
                 
            # [OPT-6] Signal deduplication: write to DB only when something meaningful
            # changes. Prevents 50+ identical LOW_CONFIDENCE rows per symbol per hour.
            # Always write: ATTEMPTED (execution), first occurrence, signal type change,
            # or confidence shift ≥ 2%. Suppress repeated SKIPPED/LOW_CONF at same level.
            conf_bucket = round(analysis.confidence, 2)
            last        = self._last_logged_signal.get(symbol)
            should_log  = (
                result_status == "ATTEMPTED"                              # always log executions
                or last is None                                            # first seen for this symbol
                or last[0] != analysis.signal                             # signal type changed
                or abs(last[1] - conf_bucket) >= 0.02                    # confidence shifted ≥2%
                or (result_status != "SKIPPED" and last[2] == "SKIPPED") # first non-neutral
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

                # [S12] ATR-based buffer for Gold/Silver — min_buffer(0.50) is too
                # small: Gold M15 ATR is 5-25pt, so SL from a 0.50 buffer fails the
                # 1.5pt guard every time. Use 0.5×ATR as Gold's volatility_buffer.
                if "XAU" in symbol or "XAG" in symbol:
                    atr_val = df['atr'].iloc[-1] if 'atr' in df.columns else volatility_buffer
                    volatility_buffer = max(atr_val * 0.5, volatility_buffer)

                magic_number = 510001 if is_nano else 510000

                # ==========================================
                # BOUNDED DYNAMIC NANO STOP LOSS
                # ==========================================
                base_nano_sl = volatility_buffer * 0.4
                floor_sl = 0.060 if "JPY" in symbol else 0.00060  
                ceil_sl = 0.150 if "JPY" in symbol else 0.00150   
                
                dynamic_nano_sl = max(floor_sl, min(base_nano_sl, ceil_sl))
                dynamic_nano_tp = dynamic_nano_sl * 2.0

                # ── Pull structural price levels from analyst conditions ──
                ict_cond     = getattr(analysis, 'ict_conditions', {}) or {}
                ob_entry     = ict_cond.get('ob_entry_price')    # OB body midpoint
                ob_zone_low  = ict_cond.get('ob_zone_low')       # OB full zone low
                ob_zone_high = ict_cond.get('ob_zone_high')      # OB full zone high
                swing_sl_ref = ict_cond.get('swing_sl_ref')      # structural swept level
                sl_atr_buf   = ict_cond.get('sl_atr_buffer', volatility_buffer * 0.1)
                tp_target    = ict_cond.get('tp_target_level')   # asian H/L or session target

                if is_buy:
                    if is_nano:
                        action = "BUY_MARKET"
                        raw_price = tick.ask
                        sl_price = tick.bid - dynamic_nano_sl
                        tp_price = tick.ask + dynamic_nano_tp
                    else:
                        action = "BUY_LIMIT"
                        # [S12-P0A] Entry: use OB body_low (institutional value zone).
                        # If no OB detected, fall back to ob_zone_low, then to
                        # the forming candle's low as last resort.
                        raw_price = ob_entry or ob_zone_low or df.iloc[-2]['low']

                        # [S12-P0B] SL: place below the swept swing low + ATR buffer.
                        # The swept level IS the liquidity that was taken — SL beneath
                        # it means the trade is invalidated only if that hunt was a fake.
                        # Fall back to df.iloc[-3]['low'] if no structural reference.
                        if swing_sl_ref is not None:
                            sl_price = swing_sl_ref - sl_atr_buf
                        else:
                            sl_price = df.iloc[-3]['low'] - (volatility_buffer * 0.1)

                        # [BUG-40 FIX] Enforce minimum SL distance from entry price.
                        # Root cause: when ob_entry is None, raw_price falls back to
                        # df.iloc[-2]['low'] which is typically JUST ABOVE swing_sl_ref.
                        # This makes sl_distance ≈ sl_atr_buf (~1-2 pips) — far below
                        # the 5-pip SL guard. Fix: expand the SL outward to the asset's
                        # minimum guard distance from entry, preserving structural
                        # placement when it's already far enough.
                        # Evidence: EURGBP sl_dist=0.00023, GBPUSD=0.00003, NZDUSD=0.00019
                        # 20% buffer above min prevents float precision boundary failure
                        # (0.86460 - 0.0005 yields 0.000499999... in IEEE 754).
                        _buy_min_sl_guard = (100.0  if "BTC"     in symbol else
                                             5.0    if "ETH"     in symbol else
                                             1.5    if "XAU"     in symbol else
                                             0.10   if "XAG"     in symbol else
                                             0.50   if "Oil"     in symbol else  # BUG-43: was 0.10 (too tight for Oil volatility)
                                             0.05   if "NGAS"    in symbol else
                                             1.0    if "SP 500"  in symbol else
                                             2.0    if ("Tech 100" in symbol or "Germany" in symbol) else
                                             0.10   if "JPY"     in symbol else
                                             0.0005)
                        _min_sl_from_entry_buy = raw_price - (_buy_min_sl_guard * 1.20)
                        if sl_price > _min_sl_from_entry_buy:
                            sl_price = _min_sl_from_entry_buy

                        # [S12-P1B] TP: target the Asian High (opposite liquidity pool).
                        # For a bullish manipulation (swept SSL), smart money now
                        # distributes toward BSL = Asian High. Use 30-bar lookback high
                        # as backup if Asian range is not available.
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
                        # [S12-P0A] Entry: use OB body_high for SELL (institutional supply zone).
                        raw_price = ob_entry or ob_zone_high or df.iloc[-2]['high']

                        # [S12-P0B] SL: place above the swept swing high + ATR buffer.
                        if swing_sl_ref is not None:
                            sl_price = swing_sl_ref + sl_atr_buf
                        else:
                            sl_price = df.iloc[-3]['high'] + (volatility_buffer * 0.1)

                        # [BUG-40 FIX] Mirror of BUY fix — enforce min SL distance from
                        # SELL entry. Structural SL (above swing high) can be within
                        # 1-2 pips of the fallback entry (df.iloc[-2]['high']).
                        # 20% buffer above min prevents float precision boundary failure.
                        _sell_min_sl_guard = (100.0  if "BTC"     in symbol else
                                              5.0    if "ETH"     in symbol else
                                              1.5    if "XAU"     in symbol else
                                              0.10   if "XAG"     in symbol else
                                              0.50   if "Oil"     in symbol else  # BUG-43: was 0.10
                                              0.05   if "NGAS"    in symbol else
                                              1.0    if "SP 500"  in symbol else
                                              2.0    if ("Tech 100" in symbol or "Germany" in symbol) else
                                              0.10   if "JPY"     in symbol else
                                              0.0005)
                        _min_sl_from_entry_sell = raw_price + (_sell_min_sl_guard * 1.20)
                        if sl_price < _min_sl_from_entry_sell:
                            sl_price = _min_sl_from_entry_sell

                        # [S12-P1B] TP: target Asian Low (swept BSL → distribute toward SSL).
                        if tp_target:
                            tp_price = tp_target
                        else:
                            tp_price = df.tail(30)['low'].min() - (volatility_buffer * 0.1)
                    
                price = self.gateway.normalize_price(symbol, raw_price)
                sl = self.gateway.normalize_price(symbol, sl_price) 
                tp = self.gateway.normalize_price(symbol, tp_price) 

                # ── LIMIT PRICE VALIDATION (BUG-27 FIX) ──────────────────────
                # MT5 rejects SELL_LIMIT if price <= ask + stops_level*point,
                # and BUY_LIMIT if price >= bid - stops_level*point.
                # Root cause: df.iloc[-1] is the CURRENTLY FORMING candle.
                # Its high/low is live market price, not a future level, so
                # on fast-moving symbols the limit was at or below market.
                # Log evidence: EURUSD/USDCHF/NZDUSD/XAUUSD/XAGUSD all
                # rejected while GBPUSD/USDCAD/AUDJPY (wider structure) passed.
                # Fix: compute minimum safe distance from the symbol's stops_level
                # and reject before sending — logs a clear reason instead of
                # letting MT5 silently eat the order.
                if not is_nano:
                    sym_info = mt5.symbol_info(self.gateway.find_symbol(symbol) or symbol)
                    if sym_info:
                        # [HOTFIX] Some brokers don't expose stops_level on SymbolInfo directly.
                        # Use getattr() with safe default — same pattern as get_symbol_properties().
                        # Without this, USDCAD/GBPJPY crashed: 'SymbolInfo' has no 'stops_level'.
                        stops_pt = getattr(sym_info, 'stops_level', 0) * sym_info.point
                        # Add a 2-point buffer on top of broker minimum
                        min_dist = stops_pt + (sym_info.point * 2)
                        if is_buy and price >= (tick.bid - min_dist):
                            self.log_info(
                                f"⚠️ Price Validation: {symbol} BUY_LIMIT {price} too close "
                                f"to market ({tick.bid}). Min distance: {min_dist:.5f}. Skipping."
                            )
                            return
                        elif not is_buy and price <= (tick.ask + min_dist):
                            self.log_info(
                                f"⚠️ Price Validation: {symbol} SELL_LIMIT {price} too close "
                                f"to market ({tick.ask}). Min distance: {min_dist:.5f}. Skipping."
                            )
                            return
                # ─────────────────────────────────────────────────────────────

                sl_distance = abs(price - sl)

                # [BUG-41 FIX] SL-to-current-price stops_level guard.
                # MT5 "Invalid stops" happens when the SL is too close to the
                # CURRENT market price — not just the limit order price. The
                # existing guard only checks |entry - sl|. But if the market has
                # moved toward sl since the signal fired (before order submission),
                # MT5 rejects with "Invalid stops" even though |entry - sl| passes.
                # Fix: also enforce |current_price - sl| >= broker stops_minimum.
                # Evidence: USDJPY 99%, USDCHF 99%, XAUUSD 92% all rejected at 17:00.
                if not is_nano:
                    try:
                        _sym_info_41 = mt5.symbol_info(self.gateway.find_symbol(symbol) or symbol)
                        if _sym_info_41:
                            _stops_pt = getattr(_sym_info_41, 'stops_level', 0) * _sym_info_41.point
                            _min_stop_dist = _stops_pt + (_sym_info_41.point * 3)   # 3pt buffer
                            if is_buy:
                                _sl_to_current = abs(tick.bid - sl)
                            else:
                                _sl_to_current = abs(tick.ask - sl)
                            if _sl_to_current < _min_stop_dist and _min_stop_dist > 0:
                                # SL would be rejected by MT5 — expand it outward
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
                        pass   # Non-fatal: original SL used if MT5 info unavailable

                # [BUG-35 FIX] Minimum SL distance guard
                # If SL is too tight, the lot formula produces dangerously large lots.
                # SL MINIMUM GUARD — prevents execution when SL is too tight to
                # survive spread + slippage, and avoids runaway lot sizes.
                # Minimums derived from Deriv asset spec: stops_level × tick_size,
                # with a 2× safety buffer on top of the absolute broker minimum.
                # Source: Assets_specification.xlsx — "Stops level" row.
                if not is_nano:
                    if "BTC" in symbol:
                        min_sl_guard = 100.0      # 100 pts on BTC
                    elif "ETH" in symbol:
                        min_sl_guard = 5.0
                    elif "XAU" in symbol:
                        min_sl_guard = 1.5        # 150 pips on Gold
                    elif "XAG" in symbol:
                        min_sl_guard = 0.10
                    elif "Oil" in symbol:
                        # spec stops_level=50, tick=0.001 → broker min=0.05.
                        # [BUG-43] Oil min guard raised from 0.10 to 0.50 to ensure
                        # meaningful SL with corrected ×100 lot formula.
                        # 0.50 SL × 100 = $50/lot → ~1 lot at $9k account ✓
                        min_sl_guard = 0.50
                    elif "NGAS" in symbol:
                        # spec stops_level=5, tick=0.001 → broker min=0.005; use 0.01
                        min_sl_guard = 0.01
                    elif "SP 500" in symbol:
                        # spec stops_level=50, tick=0.01 → broker min=0.50; use 1.0
                        min_sl_guard = 1.0
                    elif "Tech 100" in symbol or "Germany" in symbol:
                        # spec stops_level=150, tick=0.01 → broker min=1.50; use 2.0
                        min_sl_guard = 2.0
                    elif "JPY" in symbol:
                        min_sl_guard = 0.10       # 10 pips JPY pairs
                    else:
                        min_sl_guard = 0.0005     # 5 pips standard FX
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
                
                # [SPRINT 7] Kelly-informed dynamic risk scaling
                # Uses quarter-Kelly when N>=30, fixed 2% below that.
                # Confidence scaling: higher ICT score → proportionally more risk.
                quant_params    = self.quant_engine.get_live_risk_params()
                kelly_risk      = quant_params.get('risk_pct', 0.02)
                regime_mult_q   = quant_params.get('regime_multiplier', 1.0)

                # Margin guard still overrides Kelly
                margin_mult     = 0.5 if (margin_level > 0.0 and margin_level < 500.0) else 1.0
                reduction_mult  = 0.5 if self._risk_reduction_mode else 1.0

                # Confidence scaling: 80% = base risk, 99% = +25% more risk
                # Anchored to new standard threshold (0.80) so the full scaling
                # range applies across genuine signals, not a compressed 0.88-0.99 band
                conf_scale      = (analysis.confidence - 0.80) / (0.99 - 0.80)
                conf_scale      = max(0.0, min(1.0, conf_scale))
                base_risk_pct   = kelly_risk * (1.0 + 0.25 * conf_scale)

                is_micro = "MICRO" in analysis.signal
                risk_multiplier = margin_mult * reduction_mult * regime_mult_q
                if is_nano:
                    risk_multiplier = risk_multiplier * 0.25   # NANO = quarter size
                elif is_micro:
                    risk_multiplier = risk_multiplier * 0.50   # MICRO = half size 

                if "XAU" in symbol or "XAG" in symbol:
                    # Gold: 1 lot = 100 oz. Tick size=0.01, tick value=$0.01/oz/lot
                    # → $1 price move = $100/lot (100oz × $1). Capital/lot = sl × 100.
                    risk_capital    = (balance * base_risk_pct * 0.5) * risk_multiplier
                    capital_per_lot = sl_distance * 100
                    min_lot  = 0.10
                    vol_step = 0.01
                elif "BTC" in symbol:
                    # BTC CFD (Deriv): contract_size=1. Tick_size=1, tick_value=$1.
                    # $1 price move = $1/lot → capital_per_lot = sl_distance × 1.
                    risk_capital    = (balance * base_risk_pct * 0.5) * risk_multiplier
                    capital_per_lot = sl_distance * 1
                    min_lot  = 0.01
                    vol_step = 0.01
                elif "ETH" in symbol:
                    # ETH CFD (Deriv): same 1:1 structure as BTC.
                    risk_capital    = (balance * base_risk_pct * 0.5) * risk_multiplier
                    capital_per_lot = sl_distance * 1
                    min_lot  = 0.01
                    vol_step = 0.01
                elif "Oil" in symbol:
                    # US Oil (Deriv): tick_size=0.001, tick_value=$0.001, contract_size=100.
                    # A $1 price move on 1 lot = (1/0.001) × $0.001 × 100 = $100 P&L.
                    # [BUG-43 FIX] capital_per_lot = sl_distance × 100.
                    # Old formula (* 1.0) produced 860 lots on 2026-03-11 — capped at 91, still fatal.
                    # Correct: 0.069 × 100 = $6.9/lot → ~8.5 lots at $9k account ✓
                    # Verified: Assets_specification.xlsx, "US Oil" column.
                    # min_volume=1 (integer lots only), vol_step=1.
                    risk_capital    = (balance * base_risk_pct * 0.5) * risk_multiplier
                    capital_per_lot = sl_distance * 100.0  # BUG-43 fix: was * 1.0 (860 lots on 2026-03-11)
                    min_lot  = 1.0     # spec: minimal volume = 1 (not 0.1!)
                    vol_step = 1.0     # spec: volume step = 1 (integer lots only)
                elif "NGAS" in symbol:
                    # NGAS (Deriv): contract_size=10000, tick_size=0.001, tick_value=$0.001.
                    # [BUG-42 FIX] capital_per_lot = (sl/tick_size)*tick_value*contract_size
                    #   = (sl/0.001)*0.001*10000 = sl*10000. Old formula (*1.0) was wrong
                    # HARD max: spec maximal volume = 5 lots (binding constraint).
                    risk_capital    = (balance * base_risk_pct * 0.5) * risk_multiplier
                    capital_per_lot = sl_distance * 10000.0  # BUG-42 fix: was * 1.0 (5526 lots on 2026-03-11)
                    min_lot  = 0.1
                    vol_step = 0.1
                elif "SP 500" in symbol:
                    # US SP 500 (Deriv): contract_size=1, tick_size=0.01, tick_value=$0.01.
                    # tick_value/tick_size = 1.0 → capital_per_lot = sl_distance × 1.
                    # Example: 10pt SL → $10 risk/lot → $179 risk ÷ $10 = 17.9 lots.
                    # Verified: Assets_specification.xlsx, "US SP 500" column.
                    risk_capital    = (balance * base_risk_pct * 0.5) * risk_multiplier
                    capital_per_lot = sl_distance * 1.0
                    min_lot  = 0.1
                    vol_step = 0.1
                elif "Tech 100" in symbol:
                    # US Tech 100 (Deriv): same 1:1 structure as SP 500.
                    # stops_level=150 (larger than SP500's 50) → wider minimum SL.
                    risk_capital    = (balance * base_risk_pct * 0.5) * risk_multiplier
                    capital_per_lot = sl_distance * 1.0
                    min_lot  = 0.1
                    vol_step = 0.1
                elif "Germany" in symbol:
                    # Germany 40 DAX (Deriv): contract_size=1, tick=0.01, tv=$0.01.
                    # Profit currency = EUR — note this for P&L display only;
                    # risk formula is identical to SP500/Tech100.
                    risk_capital    = (balance * base_risk_pct * 0.5) * risk_multiplier
                    capital_per_lot = sl_distance * 1.0
                    min_lot  = 0.1
                    vol_step = 0.1
                elif "JPY" in symbol:
                    risk_capital    = (balance * base_risk_pct) * risk_multiplier
                    capital_per_lot = sl_distance * 1000
                    min_lot  = 0.30
                    vol_step = 0.01
                else:
                    # Standard FX: 1 lot = 100,000 units; pip = 0.0001
                    risk_capital    = (balance * base_risk_pct) * risk_multiplier
                    capital_per_lot = sl_distance * 100000
                    min_lot  = 0.30
                    vol_step = 0.01

                raw_lot        = risk_capital / capital_per_lot
                # Round DOWN to volume step (broker rejects if not aligned to step)
                import math as _math
                step_inv       = round(1.0 / vol_step)
                calculated_lot = _math.floor(raw_lot * step_inv) / step_inv
                lot = max(min_lot, calculated_lot)

                # HARD MAXIMUM LOT CAP — spec-verified per asset.
                # Internal safe caps are well below the broker's spec maximum
                # to prevent outsized positions from tight SLs or edge cases.
                # Source: Assets_specification.xlsx "Maximal volume" column.
                if "BTC" in symbol or "ETH" in symbol:
                    # scales with account: ~0.5@$9k, ~2.5@$50k (spec max not provided)
                    max_lot = max(0.5, round(balance / 20000, 2))
                elif "XAU" in symbol or "XAG" in symbol:
                    max_lot = 2.0
                elif "Oil" in symbol:
                    # [BUG-43 FIX] Previous cap scaled with balance/100 → ~91 lots at $9k.
                    # With the corrected lot formula (×100 multiplier) 91 lots = ~$750k notional.
                    # Safe cap: Oil is high-volatility commodity; max 5 lots at current account.
                    # Scales conservatively: ~1 lot per $1500 balance, hard ceiling at 10.
                    max_lot = max(1.0, min(10, round(balance / 1500, 0)))   # ~6 at $9k
                elif "NGAS" in symbol:
                    max_lot = 5.0    # HARD limit from spec: maximal volume = 5
                elif "SP 500" in symbol:
                    # spec max=150; safe internal cap
                    max_lot = max(0.1, min(30, round(balance / 500, 1)))   # ~17 at $9k
                elif "Tech 100" in symbol:
                    # spec max=100; safe internal cap
                    max_lot = max(0.1, min(20, round(balance / 500, 1)))   # ~17 at $9k
                elif "Germany" in symbol:
                    # spec max=100; same structure as Tech 100
                    max_lot = max(0.1, min(20, round(balance / 500, 1)))   # ~17 at $9k
                elif is_nano:
                    max_lot = 0.10
                elif is_micro:
                    max_lot = max(0.5, round(balance / 20000, 2))   # ~0.5 at $9k
                else:
                    max_lot = max(1.0, round(balance / 5000, 2))    # ~1.8 at $9k

                if lot > max_lot:
                    self.log_info(
                        f"⚠️ Lot Cap: {symbol} calculated {lot} lots → capped at {max_lot} "
                        f"({'MICRO' if is_micro else 'NANO' if is_nano else 'STANDARD'})"
                    )
                    lot = max_lot
                
                filling_mode_code = props.get('filling_mode', 0)
                if filling_mode_code & 1:
                    type_filling = mt5.ORDER_FILLING_FOK
                elif filling_mode_code & 2:
                    type_filling = mt5.ORDER_FILLING_IOC
                else:
                    type_filling = mt5.ORDER_FILLING_RETURN 

                request = {
                    "action": mt5.TRADE_ACTION_DEAL if is_nano else mt5.TRADE_ACTION_PENDING,
                    "symbol": symbol,
                    "volume": float(lot),
                    "price": float(price),
                    "sl": float(sl),
                    "tp": float(tp),
                    "deviation": 10,
                    "magic": magic_number,
                    "comment": "SMC_Nano" if is_nano else "SMC_Limit",
                    "type_time": mt5.ORDER_TIME_GTC if is_nano else mt5.ORDER_TIME_SPECIFIED,
                    "type_filling": type_filling,
                }

                if not is_nano: 
                    request["expiration"] = int(time.time()) + (4 * 3600)
                
                if is_buy: 
                    request["type"] = mt5.ORDER_TYPE_BUY if is_nano else mt5.ORDER_TYPE_BUY_LIMIT
                else: 
                    request["type"] = mt5.ORDER_TYPE_SELL if is_nano else mt5.ORDER_TYPE_SELL_LIMIT

                # ── SEND ORDER with filling-mode fallback (BUG-32 FIX) ────────
                # For NANO market orders, the broker's declared filling_mode
                # bitmask can be stale or change per-session (we observed 194
                # "Unsupported filling mode" rejections Mar 5 03:15-05:59 with
                # no automatic recovery).  Try FOK first (most compatible),
                # then IOC, then RETURN before giving up.
                fill_order   = [mt5.ORDER_FILLING_FOK,
                                mt5.ORDER_FILLING_IOC,
                                mt5.ORDER_FILLING_RETURN]
                fill_names   = ["FOK", "IOC", "RETURN"]
                FILL_ERR     = 10038   # TRADE_RETCODE_INVALID_FILL

                result = None
                if is_nano:
                    for idx, fmode in enumerate(fill_order):
                        request["type_filling"] = fmode
                        result = mt5.order_send(request)
                        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                            break   # success — no need to try other modes
                        if result and result.retcode != FILL_ERR:
                            break   # failure is NOT a filling-mode issue, stop retrying
                        # filling-mode mismatch — try next mode silently
                else:
                    result = mt5.order_send(request)
                # ─────────────────────────────────────────────────────────────

                safe_action = action.replace("_", " ")

                if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                    fill_label = f" | Fill: {fill_names[fill_order.index(request['type_filling'])]}" if is_nano else ""
                    self.log_info(f"⚡ {'MARKET EXECUTION' if is_nano else 'TRAP SET'}: {symbol} {action} | Entry: {price} | Lot: {lot}{fill_label}")
                    self.async_alert(f"⚡ **SMC {safe_action}**: {symbol}\nTarget Entry: {price}\nLot: {lot}\nConf: {analysis.confidence*100:.0f}%")
                    # [BUG-33 FIX] Update signal record from ATTEMPTED → FILLED
                    DBManager.update_signal_result(symbol, analysis.signal, "FILLED")

                    # [S9] TRADE RECORDING ──────────────────────────────
                    acc_id = self._get_current_account_id()
                    if is_nano:
                        # Market orders fill instantly — record now
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
                        # LIMIT order — pending until filled.  run_cycle() will
                        # detect the fill and call save_trade() at that point.
                        self._pending_order_info[result.order] = {
                            'regime':       regime,
                            'account_id':   acc_id,
                            'model_type':   'ICT_STANDARD',
                            'model_sizing': 'STANDARD',
                        }
                    # ────────────────────────────────────────────────────
                else:
                    err_msg = result.comment if result else "Unknown MT5 Error"
                    self.log_info(f"❌ MT5 REJECTED {symbol}: {err_msg}")
                    # [BUG-31 FIX] Apply symbol cooldown after ANY rejection.
                    # Previously: no cooldown set → same signal retried every 60s
                    # indefinitely.  Evidence: 194 consecutive rejections over
                    # 2h45m (Mar 5 03:15-05:59) for NZDUSD, EURUSD, GBPUSD etc.
                    # A standard 15-min cooldown prevents the retry storm while
                    # still allowing a fresh attempt when conditions change.
                    self.symbol_cooldowns[symbol] = datetime.utcnow()
                    # [BUG-33 FIX] Update signal record from ATTEMPTED → REJECTED
                    DBManager.update_signal_result(symbol, analysis.signal, f"REJECTED: {err_msg}")

            except Exception as e:
                self.log_info(f"⚠️ Thread Execution Error on {symbol}: {e}")
                # [HOTFIX] Apply cooldown on ANY exception, not just clean rejections.
                # Without this: exception path skips BUG-31 protection entirely.
                # Evidence: USDCAD/GBPJPY retried every 60s despite crashing each time.
                # The stops_level AttributeError above is fixed, but this guard ensures
                # future unknown exceptions also don't produce infinite retry cascades.
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
            # [BUG-24 FIX] kill_switch was absent from the response dict.
            # Flutter dashboard reads data['kill_switch'] to show the red
            # lockout banner — KeyError meant it silently defaulted to False
            # and the banner never appeared even during an active kill switch.
            "kill_switch": self.kill_switch_active
        }
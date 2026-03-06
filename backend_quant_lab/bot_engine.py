import os
import logging
import json
import pandas as pd
from datetime import datetime, timedelta, timezone
import time
import MetaTrader5 as mt5
from mt5_interface import MT5Gateway
from analyst import analyze_market_structure, AnalysisRequest
from models import Candle
from telegram_client import TelegramNotifier
from db_manager import DBManager
from news_manager import NewsManager
from vision_module import VisionEngine   # [BUG-16] Activated — was never imported
import threading
import math

# ──────────────────────────────────────────────────────────────
# LOGGING SETUP  (unchanged — already correct)
# ──────────────────────────────────────────────────────────────
os.makedirs("logs", exist_ok=True)
logger = logging.getLogger("TradeCoreEngine")
logger.setLevel(logging.DEBUG)

if not logger.handlers:
    from logging.handlers import RotatingFileHandler

    c_handler = logging.StreamHandler()
    c_handler.setLevel(logging.INFO)

    f_handler = RotatingFileHandler(
        "logs/tradecore_brain.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    f_handler.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    c_handler.setFormatter(fmt)
    f_handler.setFormatter(fmt)

    logger.addHandler(c_handler)
    logger.addHandler(f_handler)


# ──────────────────────────────────────────────────────────────
# NANO SPREAD LIMITS  (replaces the asset-name blacklist BUG-14)
# Keys are substrings matched against the symbol name.
# Values are maximum spread in points for NANO execution.
# ──────────────────────────────────────────────────────────────
_NANO_SPREAD_LIMITS = {
    "XAU": 80,        # Gold  — allow NANO below 80 pts spread
    "XAG": 200,       # Silver
    "BTC": 1500,      # Bitcoin
    "ETH": 500,       # Ethereum
    "US SP 500": 300, # S&P 500
    "US Tech 100": 400,# Nasdaq
}


def _utcnow() -> datetime:
    """Single source of UTC time used everywhere in this file.
    Fixes BUG-19: was mixing datetime.now(), datetime.utcnow(),
    and timezone-naive/aware datetimes inconsistently."""
    return datetime.now(tz=timezone.utc).replace(tzinfo=None)


class TradingBot:

    def __init__(self):
        self.gateway      = MT5Gateway()
        self.notifier     = TelegramNotifier()
        self.news_manager = NewsManager()

        # ── ASSET MATRIX ──────────────────────────────────────
        self.vip_assets = [
            "EURUSD", "GBPUSD", "USDJPY", "USDCAD", "USDCHF", "AUDUSD", "NZDUSD",
            "EURJPY", "GBPJPY", "EURGBP", "AUDJPY",
            "XAUUSD", "XAGUSD",
            "BTCUSD", "ETHUSD",
            "US SP 500", "US Tech 100",
        ]

        self.active_symbols  = []
        self.symbol_cooldowns = {}

        self.MAX_OPEN_TRADES  = 12
        self.MAX_SNIPER_SLOTS = 5
        self.MAX_GOLD_TRADES  = 3

        self.logs          = []
        self.is_running    = False
        # [BUG-03] active_tickets REMOVED entirely.
        # process_symbol() was checking this set and returning
        # immediately for any symbol with an existing position,
        # making all per-symbol multi-position logic unreachable.
        # execution_lock is the correct and sufficient guard.
        self.execution_lock = set()

        self.state_file      = "logs/tradecore_state.json"
        self.scaled_positions = self._load_state()

        self.daily_start_balance   = 0.0
        self.last_trade_day        = -1
        self.kill_switch_active    = False
        # [BUG-12] Timestamp recorded when kill switch fires.
        # Reset is based on 8-hour elapsed time, not UTC day boundary.
        self.kill_switch_triggered_at: datetime | None = None

        self.current_var   = 0.0
        self.market_regime = "CALIBRATING..."

    # ──────────────────────────────────────────────────────────
    # STATE PERSISTENCE
    # ──────────────────────────────────────────────────────────
    def _load_state(self) -> set:
        try:
            if os.path.exists(self.state_file):
                with open(self.state_file, "r") as f:
                    data = json.load(f)
                    return set(data.get("scaled_positions", []))
        except Exception as e:
            self.log_debug(f"State Load Error: {e}")
        return set()

    def _save_state(self):
        try:
            with open(self.state_file, "w") as f:
                json.dump({"scaled_positions": list(self.scaled_positions)}, f)
        except Exception as e:
            self.log_debug(f"State Save Error: {e}")

    # ──────────────────────────────────────────────────────────
    # LOGGING
    # ──────────────────────────────────────────────────────────
    def log_info(self, message: str):
        logger.info(message)
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.logs.insert(0, f"[{timestamp}] {message}")
        if len(self.logs) > 100:
            self.logs.pop()

    def log_debug(self, message: str):
        logger.debug(message)

    def async_alert(self, msg: str):
        """Non-blocking Telegram message dispatch."""
        def _send():
            try:
                self.notifier.send(msg)
            except Exception:
                pass
        threading.Thread(target=_send, daemon=True).start()

    # ──────────────────────────────────────────────────────────
    # TELEGRAM COMMAND HANDLER
    # ──────────────────────────────────────────────────────────
    def handle_telegram_command(self, command: str):
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
                    icon = "🔴" if item["impact"] == "High" else "🟠"
                    tier_tag = f"[T{item.get('tier', 2)}]"
                    lines.append(f"{icon} {tier_tag} {item['time']} • {item['country']} {item['title']}")
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
                self.async_alert(
                    f"💰 **Balance:** ${acc['balance']:.2f}\n"
                    f"**Equity:** ${acc['equity']:.2f}"
                )
        elif cmd == "/killswitch":
            remaining = ""
            if self.kill_switch_active and self.kill_switch_triggered_at:
                elapsed = (_utcnow() - self.kill_switch_triggered_at).total_seconds()
                mins_left = max(0, int((28800 - elapsed) / 60))
                remaining = f"\nResumes in ≈{mins_left} minutes."
            status = "🔴 ACTIVE" if self.kill_switch_active else "🟢 INACTIVE"
            self.async_alert(f"🛑 **Kill Switch:** {status}{remaining}")

    def _report_status(self):
        acc = self.gateway.get_account_info()
        if acc:
            balance      = acc["balance"]
            equity       = acc["equity"]
            free_margin  = acc["free_margin"]
            margin_level = acc.get("margin_level", 0.0)
        else:
            balance = equity = free_margin = margin_level = 0.0

        positions     = self.gateway.get_open_positions()
        total_profit  = sum(p["profit"] for p in positions)

        msg = (
            f"📊 **TradeCore v51.0 Status**\n"
            f"─────────────────────────\n"
            f"💰 Balance: ${balance:,.2f}\n"
            f"📈 Equity: ${equity:,.2f}\n"
            f"🛡️ Free Margin: ${free_margin:,.2f}\n"
            f"⚡ Margin Level: {margin_level:.2f}%\n"
            f"📊 Regime: {self.market_regime}\n"
            f"🎯 Daily VaR Limit: ${self.current_var:.2f}\n"
            f"─────────────────────────\n"
            f"🔓 Active Trades: {len(positions)}\n"
            f"⏳ Pending Locks: {len(self.execution_lock)}\n"
            f"💵 Floating PnL: ${total_profit:,.2f}\n"
        )
        if positions:
            msg += "─────────────────────────\n"
            for p in positions:
                icon = "🟢" if p["profit"] >= 0 else "🔴"
                msg += f"{icon} {p['symbol']} ({p['type']}): ${p['profit']:.2f}\n"

        self.async_alert(msg)

    # ──────────────────────────────────────────────────────────
    # SERVICE LIFECYCLE
    # ──────────────────────────────────────────────────────────
    def start_service(self) -> bool:
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
        self.log_info(
            f"✅ TradeCore v51.0: Engine Active. "
            f"Monitoring {len(self.active_symbols)} Assets."
        )

        self.news_manager.fetch_calendar()
        self.notifier.start_listening(self.handle_telegram_command)
        self.async_alert(
            "🚀 **TradeCore v51.0 Online**\n"
            "Dynamic Structural Targets Armed.\n"
            "All sprint repairs active."
        )
        return True

    def stop_service(self):
        self.is_running = False
        self.notifier.stop_listening()
        self.log_info("Engine stopped.")

    # ──────────────────────────────────────────────────────────
    # MARKET SCHEDULE
    # ──────────────────────────────────────────────────────────
    def check_market_schedule(self) -> tuple[bool, str]:
        now    = _utcnow()   # [BUG-19] unified UTC
        day    = now.weekday()
        hour   = now.hour
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

    # ──────────────────────────────────────────────────────────
    # POSITION MANAGEMENT HELPERS
    # ──────────────────────────────────────────────────────────
    def close_all_positions(self, positions: list):
        for pos in positions:
            self.gateway.close_position(
                pos["ticket"], pos["symbol"], pos["volume"], pos["type"]
            )

    def evaluate_pending_orders(self):
        orders = mt5.orders_get()
        if not orders:
            return

        for ord in orders:
            symbol     = ord.symbol
            ticket     = ord.ticket
            order_type = ord.type
            tp         = ord.tp
            sl         = ord.sl

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
                    self.log_info(
                        f"🗑️ Stale Trap Avoided: Cancelled {symbol} limit order. ({reason})"
                    )

    def evaluate_open_positions(self, positions: list):
        """
        12-hour dead momentum liquidation.
        [BUG-09 FIX] Uses pos['open_time'] which is now correctly
        included by mt5_interface.get_open_positions(). Previously
        the 'time' key was missing — KeyError silently swallowed
        every cycle, meaning this block NEVER executed.
        """
        for pos in positions:
            try:
                symbol       = pos["symbol"]
                duration_hrs = (time.time() - pos["open_time"]) / 3600.0
                profit       = pos["profit"]

                if duration_hrs > 12.0 and profit < 0:
                    self.log_info(
                        f"⏳ Time Decay Killswitch: {symbol} in dead momentum "
                        f">{duration_hrs:.1f}h. Liquidating."
                    )
                    self.gateway.close_position(
                        pos["ticket"], symbol, pos["volume"], pos["type"]
                    )
                    self.async_alert(
                        f"⏳ **Dead Momentum Liquidated:** {symbol}\n"
                        f"Trade held {duration_hrs:.1f}h with no progress. Margin freed."
                    )
                    # [BUG-19] Use consistent datetime for cooldowns
                    self.symbol_cooldowns[symbol] = _utcnow()

            except KeyError as e:
                self.log_debug(f"evaluate_open_positions KeyError on {pos.get('symbol','?')}: {e}")
            except Exception as e:
                self.log_debug(f"evaluate_open_positions error: {e}")

    # ──────────────────────────────────────────────────────────
    # RISK METRICS  (VaR + Regime)
    # [BUG-13 FIX] Now blends EURUSD + XAUUSD volatility instead
    # of using EURUSD alone. Gold's daily range is 3-5x larger
    # than FX — ignoring it caused severe VaR underestimation
    # whenever gold positions were the dominant exposure.
    # ──────────────────────────────────────────────────────────
    def evaluate_risk_metrics(self) -> tuple[str, float]:
        try:
            vol_pcts = []

            # FX volatility (primary reference)
            df_fx = self.gateway.get_market_data("EURUSD")
            if not df_fx.empty and len(df_fx) >= 15:
                hi  = df_fx["high"].iloc[-15:].max()
                lo  = df_fx["low"].iloc[-15:].min()
                mid = df_fx["close"].iloc[-1]
                vol_pcts.append(("FX", (hi - lo) / mid, 1.0))

            # Gold volatility — weighted at 0.6 (large absolute moves,
            # different scale from FX pip values)
            df_gold = self.gateway.get_market_data("XAUUSD")
            if not df_gold.empty and len(df_gold) >= 15:
                hi  = df_gold["high"].iloc[-15:].max()
                lo  = df_gold["low"].iloc[-15:].min()
                mid = df_gold["close"].iloc[-1]
                vol_pcts.append(("GOLD", (hi - lo) / mid, 0.6))

            if not vol_pcts:
                return "CALIBRATING...", 0.0

            # Weighted blend
            total_weight = sum(w for _, _, w in vol_pcts)
            blended_vol  = sum(v * w for _, v, w in vol_pcts) / total_weight

            self.log_debug(
                "VaR inputs: " +
                " | ".join(f"{name} {v*100:.3f}%" for name, v, _ in vol_pcts) +
                f" → blended {blended_vol*100:.3f}%"
            )

            if blended_vol > 0.008:
                regime = "HIGH VOLATILITY (GARCH)"
            elif blended_vol > 0.003:
                regime = "NORMAL (TRENDING)"
            else:
                regime = "DEAD MARKET"

            acc     = self.gateway.get_account_info()
            balance = acc["balance"] if acc else 10000.0

            raw_var      = balance * 0.15 * 2.326 * (blended_vol * 100)
            daily_var    = max(balance * 0.05, min(raw_var, balance * 0.25))

            return regime, round(daily_var, 2)

        except Exception as e:
            self.log_debug(f"⚠️ VaR Calculation Error: {e}")
            return "UNKNOWN", 0.0

    # ──────────────────────────────────────────────────────────
    # MAIN CYCLE
    # ──────────────────────────────────────────────────────────
    def run_cycle(self):
        if not self.is_running:
            return

        acc = self.gateway.get_account_info()
        if not acc:
            return

        DBManager.log_snapshot(
            acc["balance"], acc["equity"],
            acc["margin_level"], acc["free_margin"]
        )

        current_positions = self.gateway.get_open_positions()
        now_utc           = _utcnow()   # [BUG-19] single UTC reference for this cycle

        # ── KILL SWITCH CHECK ──────────────────────────────────
        # [BUG-12 FIX] Reset is now based on 8-hour elapsed time,
        # not UTC day boundary. Previously a kill switch at 23:58 UTC
        # would reset at 00:00 UTC — just 2 minutes later.
        if self.kill_switch_active:
            if self.kill_switch_triggered_at is not None:
                elapsed_secs = (now_utc - self.kill_switch_triggered_at).total_seconds()
                if elapsed_secs >= 28800:  # 8 hours
                    self.log_info(
                        f"🌅 8-Hour Kill Switch Lockout Complete. "
                        f"Resuming trading. (Was locked for {elapsed_secs/3600:.1f}h)"
                    )
                    self.async_alert(
                        "🌅 **Kill Switch Reset**\n"
                        "8-hour lockout complete. Trading resumed."
                    )
                    self.kill_switch_active       = False
                    self.kill_switch_triggered_at = None
                    self.daily_start_balance      = acc["balance"]
                else:
                    mins_remaining = int((28800 - elapsed_secs) / 60)
                    if now_utc.minute % 30 == 0 and now_utc.second < 5:
                        self.log_info(
                            f"🛑 Kill Switch Active. "
                            f"≈{mins_remaining} min remaining in lockout."
                        )
                    return
            else:
                # Edge case: triggered_at not set — use current time as base
                self.kill_switch_triggered_at = now_utc
                return

        # ── DAILY BALANCE RESET ────────────────────────────────
        current_day = now_utc.day
        if current_day != self.last_trade_day:
            self.daily_start_balance = acc["balance"]
            self.last_trade_day      = current_day

        # ── REGIME + VAR ───────────────────────────────────────
        self.market_regime, self.current_var = self.evaluate_risk_metrics()

        if self.daily_start_balance > 0 and self.current_var > 0:
            current_dd_usd = self.daily_start_balance - acc["equity"]
            if current_dd_usd >= self.current_var:
                self.log_info(
                    f"🛑 KILL SWITCH TRIGGERED: VaR Limit Breached! "
                    f"(DD: ${current_dd_usd:.2f} | Limit: ${self.current_var:.2f})"
                )
                self.async_alert(
                    f"🛑 **CRITICAL: VaR BREACHED**\n"
                    f"Drawdown ${current_dd_usd:.2f} exceeded limit ${self.current_var:.2f}.\n"
                    f"Liquidating {len(current_positions)} position(s). 8-hour lockout begins."
                )
                self.close_all_positions(current_positions)
                self.kill_switch_active       = True
                self.kill_switch_triggered_at = now_utc   # [BUG-12] record trigger time
                return

        # ── MARKET HOURS ───────────────────────────────────────
        is_open, market_status = self.check_market_schedule()
        if not is_open:
            if now_utc.minute % 30 == 0 and now_utc.second < 5:
                self.log_info(f"💤 Market Offline: {market_status}. Standby.")
            return

        # ── POSITION MANAGEMENT ────────────────────────────────
        self.apply_trailing_stop(current_positions)
        self.evaluate_pending_orders()
        self.evaluate_open_positions(current_positions)

        # [BUG-03] active_tickets assignment REMOVED.
        # This set was used in process_symbol() to block any symbol
        # that already had 1+ open positions — making multi-position
        # logic completely unreachable. It has been deleted entirely.

        # ── CAPACITY CHECKS ────────────────────────────────────
        gold_trades   = sum(1 for p in current_positions if "XAU" in p["symbol"] or "XAG" in p["symbol"])
        current_count = len(current_positions) + len(self.execution_lock)

        raw_orders   = mt5.orders_get()
        pending_list = list(raw_orders) if raw_orders else []
        usd_exposure_base = (
            len([p for p in current_positions if "USD" in p["symbol"]]) +
            len([o for o in pending_list if "USD" in o.symbol])
        )

        if current_count >= (self.MAX_OPEN_TRADES + self.MAX_SNIPER_SLOTS):
            if now_utc.second < 5:
                self.log_info(
                    f"⏸️ Absolute Capacity Full "
                    f"({current_count}/{self.MAX_OPEN_TRADES + self.MAX_SNIPER_SLOTS})."
                )
            return

        is_sniper_mode = current_count >= self.MAX_OPEN_TRADES
        if is_sniper_mode and now_utc.second < 5:
            self.log_info("🎯 Sniper Mode Active — Confidence threshold: 92%+")

        upcoming_news = self.news_manager.get_upcoming_news() if self.news_manager else []

        # ── SYMBOL LOOP ────────────────────────────────────────
        for symbol in self.active_symbols:
            # Cooldown gate — [BUG-19] use _utcnow() for comparison
            if symbol in self.symbol_cooldowns:
                if (_utcnow() - self.symbol_cooldowns[symbol]) < timedelta(minutes=60):
                    continue

            # USD exposure cap
            current_usd_locks = sum(1 for s in self.execution_lock if "USD" in s)
            if "USD" in symbol and (usd_exposure_base + current_usd_locks) >= 2:
                continue

            # Per-symbol capacity (multi-position allowed up to limit)
            symbol_trades = (
                sum(1 for p in current_positions if p["symbol"] == symbol) +
                (1 if symbol in self.execution_lock else 0)
            )

            nano_trades = sum(
                1 for p in current_positions
                if p["symbol"] == symbol and p.get("magic", 510000) == 510001
            )
            if "DEAD MARKET" in self.market_regime and nano_trades >= 1:
                continue

            if "XAU" in symbol or "XAG" in symbol:
                limit = (self.MAX_GOLD_TRADES + 1) if is_sniper_mode else self.MAX_GOLD_TRADES
                if gold_trades >= limit:
                    continue

            if is_sniper_mode and symbol_trades >= 3:
                continue
            elif not is_sniper_mode and symbol_trades >= 2:
                continue

            self.process_symbol(symbol, is_sniper_mode, upcoming_news)

    # ──────────────────────────────────────────────────────────
    # TRAILING STOP  (unchanged logic, kept intact)
    # ──────────────────────────────────────────────────────────
    def apply_trailing_stop(self, positions: list):
        for pos in positions:
            try:
                symbol     = pos["symbol"]
                ticket     = pos["ticket"]
                magic      = pos.get("magic", 510000)
                if "open_price" not in pos:
                    continue

                tick = mt5.symbol_info_tick(symbol)
                if not tick:
                    continue

                props = self.gateway.get_symbol_properties(symbol)
                min_stop_dist = (
                    (props.get("stops_level", 0) * props["point"]) + (props["point"] * 10)
                ) if props else 0.0

                vol_step = props.get("volume_step", 0.01) if props else 0.01
                min_lot  = props.get("min_lot", 0.01)     if props else 0.01

                is_buy       = pos["type"] == "BUY"
                open_price   = pos["open_price"]
                current_sl   = pos.get("sl", 0.0)
                current_vol  = pos.get("volume", 0.0)
                price_curr   = tick.bid if is_buy else tick.ask
                profit_dist  = (price_curr - open_price) if is_buy else (open_price - price_curr)
                lock_price   = 0.0
                scale_key    = f"{symbol}_{open_price}_{pos['type']}"

                # ── SCALE-OUT AT 1:1 RR ───────────────────────
                is_ready_to_scale = (
                    (("XAU" in symbol or "XAG" in symbol)                   and profit_dist > 2.0)   or
                    ("JPY" in symbol                                          and profit_dist > 0.200) or
                    (("BTC" in symbol or "ETH" in symbol or
                      "US SP 500" in symbol or "US Tech 100" in symbol)      and profit_dist > 50.0)  or
                    (not any(x in symbol for x in ["XAU","XAG","JPY","BTC","ETH","US SP 500","US Tech 100"])
                                                                              and profit_dist > 0.0020)
                )

                if is_ready_to_scale and scale_key not in self.scaled_positions:
                    half_vol  = current_vol / 2.0
                    close_vol = round(half_vol / vol_step) * vol_step

                    if close_vol >= min_lot:
                        self.log_info(
                            f"⚖️ Scaling Out: {symbol} hit 1:1 RR. "
                            f"Closing {close_vol} lots."
                        )
                        success = self.gateway.close_position(
                            ticket, symbol, close_vol, pos["type"]
                        )
                        if success:
                            self.scaled_positions.add(scale_key)
                            self._save_state()
                            self.async_alert(
                                f"⚖️ **Partial Take Profit:** {symbol}\n"
                                f"Secured 50% volume. SL → breakeven."
                            )
                            buf       = props["point"] * 5 if props else 0.0
                            lock_price = (open_price + buf) if is_buy else (open_price - buf)
                    else:
                        self.scaled_positions.add(scale_key)
                        self._save_state()

                # ── NANO TRAILING ─────────────────────────────
                if magic == 510001:
                    nano_trigger = 0.030 if "JPY" in symbol else 0.00030
                    if profit_dist > nano_trigger:
                        secured = profit_dist * 0.80
                        lock_price = (open_price + secured) if is_buy else (open_price - secured)

                # ── STANDARD TRAILING ─────────────────────────
                else:
                    if "XAU" in symbol or "XAG" in symbol:
                        if profit_dist > 5.0:
                            lock_price = (open_price + profit_dist * 0.70) if is_buy else (open_price - profit_dist * 0.70)
                        elif profit_dist > 2.0:
                            lock_price = (open_price + profit_dist * 0.50) if is_buy else (open_price - profit_dist * 0.50)
                    elif "BTC" in symbol or "ETH" in symbol or "US SP 500" in symbol or "US Tech 100" in symbol:
                        if profit_dist > 100.0:
                            lock_price = (open_price + profit_dist * 0.80) if is_buy else (open_price - profit_dist * 0.80)
                        elif profit_dist > 50.0:
                            lock_price = (open_price + profit_dist * 0.50) if is_buy else (open_price - profit_dist * 0.50)
                    elif "JPY" in symbol:
                        if profit_dist > 0.400:
                            lock_price = (open_price + profit_dist * 0.75) if is_buy else (open_price - profit_dist * 0.75)
                        elif profit_dist > 0.200:
                            lock_price = (open_price + profit_dist * 0.50) if is_buy else (open_price - profit_dist * 0.50)
                    else:
                        if profit_dist > 0.0040:
                            lock_price = (open_price + profit_dist * 0.80) if is_buy else (open_price - profit_dist * 0.80)
                        elif profit_dist > 0.0020:
                            lock_price = (open_price + profit_dist * 0.50) if is_buy else (open_price - profit_dist * 0.50)

                if lock_price == 0:
                    continue

                # Clamp to broker's minimum stop distance
                if is_buy:
                    max_allowed = price_curr - min_stop_dist
                    if lock_price > max_allowed:
                        lock_price = max_allowed
                else:
                    min_allowed = price_curr + min_stop_dist
                    if lock_price < min_allowed:
                        lock_price = min_allowed

                should_modify = (
                    current_sl == 0 or
                    (is_buy  and lock_price > current_sl) or
                    (not is_buy and lock_price < current_sl)
                )

                if should_modify:
                    lock_price = self.gateway.normalize_price(symbol, lock_price)
                    req = {
                        "action":   mt5.TRADE_ACTION_SLTP,
                        "position": ticket,
                        "sl":       lock_price,
                        "tp":       pos.get("tp", 0.0),
                    }
                    res = mt5.order_send(req)
                    if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                        self.log_info(
                            f"🛡️ Dynamic Profit Locked: {symbol} SL → {lock_price}"
                        )

            except Exception:
                pass

    # ──────────────────────────────────────────────────────────
    # PROCESS SYMBOL
    # ──────────────────────────────────────────────────────────
    def process_symbol(self, symbol: str, is_sniper_mode: bool = False,
                       upcoming_news: list = None):
        now = _utcnow()   # [BUG-19] consistent UTC
        if upcoming_news is None:
            upcoming_news = []

        # ── NEWS GUARD ────────────────────────────────────────
        # [BUG-04 FIX] Uses event_dt (datetime object) stored by
        # news_manager at fetch time. The old code tried to parse
        # the raw string here with strptime — which always raised
        # ValueError due to a format mismatch, silently caught.
        # News blocking had never fired in production.
        for event in upcoming_news:
            if event.get("impact") != "High":
                continue
            event_dt = event.get("event_dt")
            if event_dt is None:
                continue
            tier     = event.get("tier", 2)
            # Tier 1 (NFP, FOMC, CPI): 4h pre + 30min post
            # Tier 2 (PMI, retail etc): 15min pre + 15min post
            pre_sec  = 14400 if tier == 1 else 900
            post_sec = 1800  if tier == 1 else 900
            diff_sec = (event_dt - now).total_seconds()
            if -post_sec <= diff_sec <= pre_sec:
                if now.second < 5:
                    self.log_info(
                        f"📰 News Guard [{event['country']} T{tier}]: "
                        f"Blocking {symbol} — {event['title']}"
                    )
                return

        # ── EXECUTION LOCK (only guard needed — BUG-03 removed active_tickets) ──
        if symbol in self.execution_lock:
            return

        # ── SPREAD CHECK ─────────────────────────────────────
        props = self.gateway.get_symbol_properties(symbol)
        if not props:
            return

        spread = (props["ask"] - props["bid"]) / props["point"]

        if   "BTC" in symbol or "ETH" in symbol:             limit = 50000
        elif "US SP 500" in symbol or "US Tech 100" in symbol: limit = 5000
        elif "XAU" in symbol or "XAG" in symbol:             limit = 1000
        else:                                                  limit = 60

        if spread > limit:
            return

        # ── PENDING ORDER GATE ───────────────────────────────
        pending_orders = mt5.orders_get(symbol=symbol)
        if pending_orders and len(pending_orders) > 0:
            return

        # ── MARKET DATA ──────────────────────────────────────
        df_micro = self.gateway.get_market_data(symbol, timeframe=mt5.TIMEFRAME_M15)
        df_macro = self.gateway.get_market_data(symbol, timeframe=mt5.TIMEFRAME_H4)
        if df_micro.empty or df_macro.empty:
            return

        try:
            candles_micro = [
                Candle(**row) for row in df_micro.to_dict("records")
                if hasattr(row["time"], "year")
            ]
            req      = AnalysisRequest(symbol=symbol, candles=candles_micro, daily_trend="NEUTRAL")
            analysis = analyze_market_structure(req, df_macro=df_macro, market_regime=self.market_regime)

            result_status = "SKIPPED"
            required_conf = 0.92 if is_sniper_mode else 0.88

            if analysis.signal != "NEUTRAL":
                is_nano = "NANO" in analysis.signal

                # [BUG-14 FIX] NANO spread check is now a real spread comparison
                # against per-asset-class limits. The old code was a plain string
                # match on the symbol name — it never looked at the actual spread.
                if is_nano:
                    for asset_key, max_spread in _NANO_SPREAD_LIMITS.items():
                        if asset_key in symbol and spread > max_spread:
                            self.log_debug(
                                f"[{symbol}] NANO LOCK: Spread {spread:.1f}pts "
                                f"> {max_spread}pts limit."
                            )
                            return

                if analysis.confidence >= required_conf:
                    mode_tag = "SNIPER OVERRIDE" if is_sniper_mode else "MTF CONFLUENCE"
                    self.log_info(
                        f"🔎 {mode_tag}: {symbol} {analysis.signal} "
                        f"(Conf: {analysis.confidence * 100:.0f}%)"
                    )
                    result_status = "EXECUTED"
                    self.execute_signal(symbol, analysis, df_micro, props)
                else:
                    result_status = f"LOW_CONFIDENCE ({analysis.confidence * 100:.0f}%)"
                    self.log_debug(f"[{symbol}] {analysis.reason}")
            else:
                self.log_debug(f"[{symbol}] {analysis.reason}")

            indicators = {
                "trend":  "MTF_Managed",
                "reason": getattr(analysis, "reason", ""),
                "spread": round(spread, 1),
            }
            DBManager.log_signal(
                symbol, analysis.signal, analysis.confidence,
                indicators, result_status
            )

        except Exception as e:
            self.log_debug(f"Process Error on {symbol}: {e}")

    # ──────────────────────────────────────────────────────────
    # EXECUTE SIGNAL
    # ──────────────────────────────────────────────────────────
    def execute_signal(self, symbol: str, analysis, df: pd.DataFrame, props: dict):
        if symbol in self.execution_lock:
            return

        self.execution_lock.add(symbol)

        def _async_execute():
            try:
                is_nano = "NANO" in analysis.signal
                is_buy  = "BUY"  in analysis.signal

                tick = mt5.symbol_info_tick(symbol)
                if not tick:
                    return

                local_high      = df.tail(15)["high"].max()
                local_low       = df.tail(15)["low"].min()
                structure_range = local_high - local_low

                min_buffer = (
                    10.0    if "BTC" in symbol else
                    2.0     if "ETH" in symbol else
                    0.50    if "XAU" in symbol else
                    0.10    if "JPY" in symbol else
                    0.0010
                )
                volatility_buffer = max(structure_range, min_buffer)
                magic_number      = 510001 if is_nano else 510000

                # ── BOUNDED DYNAMIC NANO SL ───────────────────
                base_nano_sl  = volatility_buffer * 0.4
                floor_sl      = 0.060  if "JPY" in symbol else 0.00060
                ceil_sl       = 0.150  if "JPY" in symbol else 0.00150
                dynamic_nano_sl = max(floor_sl, min(base_nano_sl, ceil_sl))
                dynamic_nano_tp = dynamic_nano_sl * 1.5

                # ── PRICE LEVELS ──────────────────────────────
                if is_buy:
                    if is_nano:
                        action    = "BUY_MARKET"
                        raw_price = tick.ask
                        sl_price  = tick.bid - dynamic_nano_sl
                        tp_price  = tick.ask + dynamic_nano_tp
                    else:
                        action    = "BUY_LIMIT"
                        raw_price = df.iloc[-1]["low"]
                        sl_price  = df.iloc[-3]["low"] - (volatility_buffer * 0.1)
                        tp_price  = local_high + (volatility_buffer * 0.2)
                else:
                    if is_nano:
                        action    = "SELL_MARKET"
                        raw_price = tick.bid
                        sl_price  = tick.ask + dynamic_nano_sl
                        tp_price  = tick.bid - dynamic_nano_tp
                    else:
                        action    = "SELL_LIMIT"
                        raw_price = df.iloc[-1]["high"]
                        sl_price  = df.iloc[-3]["high"] + (volatility_buffer * 0.1)
                        tp_price  = local_low - (volatility_buffer * 0.2)

                price       = self.gateway.normalize_price(symbol, raw_price)
                sl          = self.gateway.normalize_price(symbol, sl_price)
                tp          = self.gateway.normalize_price(symbol, tp_price)
                sl_distance = abs(price - sl)

                # ── LIMIT PRICE VALIDATION ────────────────────
                # [HF-B] retcode 10015 "Invalid price" fired on every non-NANO
                # order because market moved past the limit price between candle
                # close and order submission. Broker rejects SELL_LIMIT below
                # current ask and BUY_LIMIT above current bid.
                # Guard: add a 3-point buffer so we only reject truly crossed prices.
                if not is_nano:
                    buf = props.get("point", 0.00001) * 3
                    if is_buy and price >= (tick.bid - buf):
                        self.log_debug(
                            f"[{symbol}] BUY_LIMIT skipped: "
                            f"entry {price} >= bid {tick.bid:.5f} (price already above entry)"
                        )
                        return
                    if not is_buy and price <= (tick.ask + buf):
                        self.log_debug(
                            f"[{symbol}] SELL_LIMIT skipped: "
                            f"entry {price} <= ask {tick.ask:.5f} (price already below entry)"
                        )
                        return

                # ── MARGIN ARMOR ──────────────────────────────
                acc_info     = self.gateway.get_account_info()
                balance      = acc_info["balance"]      if acc_info else 10000.0
                free_margin  = acc_info["free_margin"]  if acc_info else 10000.0
                margin_level = acc_info.get("margin_level", 0.0) if acc_info else 0.0

                if 0.0 < margin_level < 300.0:
                    self.log_info(
                        f"🛡️ Margin Armor: Blocking {symbol} "
                        f"(Margin Level: {margin_level:.2f}%)"
                    )
                    return

                if free_margin < (balance * 0.15):
                    self.log_info(
                        f"⚠️ Margin Alert: Blocking {symbol} "
                        f"(Free Margin: ${free_margin:.2f})"
                    )
                    return

                # ── LOT SIZING ────────────────────────────────
                risk_multiplier = 0.5 if (0.0 < margin_level < 500.0) else 1.0
                if is_nano:
                    risk_multiplier *= 0.25

                if "XAU" in symbol or "XAG" in symbol:
                    risk_capital   = (balance * 0.01) * risk_multiplier
                    capital_per_lot = sl_distance * 100
                    min_lot        = 0.20
                elif "JPY" in symbol:
                    risk_capital   = (balance * 0.02) * risk_multiplier
                    capital_per_lot = sl_distance * 1000
                    min_lot        = 0.30
                else:
                    risk_capital   = (balance * 0.02) * risk_multiplier
                    capital_per_lot = sl_distance * 100000
                    min_lot        = 0.30

                calculated_lot = round(risk_capital / capital_per_lot, 2) if capital_per_lot > 0 else min_lot
                lot            = max(min_lot, calculated_lot)

                # ── BUILD ORDER REQUEST ───────────────────────
                request = {
                    "action":       mt5.TRADE_ACTION_DEAL if is_nano else mt5.TRADE_ACTION_PENDING,
                    "symbol":       symbol,
                    "volume":       float(lot),
                    "price":        float(price),
                    "sl":           float(sl),
                    "tp":           float(tp),
                    "deviation":    10,
                    "magic":        magic_number,
                    "comment":      "SMC_Nano" if is_nano else "SMC_Limit",
                    "type_time":    mt5.ORDER_TIME_GTC if is_nano else mt5.ORDER_TIME_SPECIFIED,
                }
                if not is_nano:
                    request["expiration"] = int(time.time()) + (4 * 3600)

                request["type"] = (
                    (mt5.ORDER_TYPE_BUY  if is_nano else mt5.ORDER_TYPE_BUY_LIMIT)  if is_buy else
                    (mt5.ORDER_TYPE_SELL if is_nano else mt5.ORDER_TYPE_SELL_LIMIT)
                )

                # ── FILL-MODE RETRY LOOP ──────────────────────
                # [BUG-05 FIX] The old code determined fill mode once and called
                # order_send() exactly once with no retry. If the broker rejected
                # with retcode 10030 (unsupported fill mode), the signal was dropped.
                # The next cycle picked the same fill mode and failed again.
                # This caused 400+ consecutive rejections in the production logs.
                fill_modes = [
                    mt5.ORDER_FILLING_FOK,
                    mt5.ORDER_FILLING_IOC,
                    mt5.ORDER_FILLING_RETURN,
                ]
                result = None
                for fill_mode in fill_modes:
                    request["type_filling"] = fill_mode
                    result = mt5.order_send(request)

                    if result is None:
                        continue
                    if result.retcode == mt5.TRADE_RETCODE_DONE:
                        break                # Success — stop trying
                    elif result.retcode == 10030:
                        continue             # Unsupported fill — try next mode
                    else:
                        break                # Real error — do not retry

                safe_action = action.replace("_", " ")

                if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                    _fill_names = {0: "FOK", 1: "IOC", 2: "RETURN"}
                    _fill_label  = _fill_names.get(request["type_filling"], str(request["type_filling"]))
                    entry_log = (
                        f"⚡ {'MARKET EXECUTION' if is_nano else 'TRAP SET'}: "
                        f"{symbol} {action} | Entry: {price} | Lot: {lot} | "
                        f"Fill: {_fill_label}"
                    )
                    self.log_info(entry_log)

                    # [BUG-02 FIX] Record trade in SQLite ledger.
                    # Previously save_trade() was never called — the trades
                    # table was permanently empty, breaking sync_db, report.py,
                    # portfolio_tracker, and all analytics downstream.
                    slippage = abs(result.price - price) if hasattr(result, "price") else 0.0
                    DBManager.save_trade(
                        ticket     = result.order,
                        symbol     = symbol,
                        type_op    = "BUY" if is_buy else "SELL",
                        vol        = lot,
                        open_price = price,
                        sl         = sl,
                        tp         = tp,
                        open_time  = datetime.now(),
                        slippage   = slippage,
                        comment    = "SMC_Nano" if is_nano else "SMC_Limit",
                    )

                    self.async_alert(
                        f"⚡ **SMC {safe_action}**: {symbol}\n"
                        f"Entry: {price} | Lot: {lot}\n"
                        f"SL: {sl} | TP: {tp}\n"
                        f"Conf: {analysis.confidence * 100:.0f}%"
                    )

                    # [BUG-16] VisionEngine chart snapshot via Telegram.
                    # [HF-D] Cleanup now happens via on_complete callback that
                    # fires AFTER the file handle is fully closed by send_photo().
                    # Previously cleanup_snapshot() was called immediately after
                    # send_photo() returned, while Telegram's upload thread still
                    # had the file open → WinError 32 on Windows.
                    try:
                        snapshot = VisionEngine.generate_trade_snapshot(
                            df, symbol, action, price, sl, tp, analysis.confidence
                        )
                        if snapshot:
                            def _cleanup(path=snapshot):
                                try:
                                    VisionEngine.cleanup_snapshot(path)
                                except Exception:
                                    pass  # Already logged by VisionEngine
                            self.notifier.send_photo(
                                snapshot,
                                caption=f"📊 {symbol} {safe_action} | Conf: {analysis.confidence * 100:.0f}%",
                                on_complete=_cleanup,
                            )
                    except Exception as ve:
                        self.log_debug(f"VisionEngine non-critical error: {ve}")

                else:
                    err_msg = result.comment if result else "Unknown MT5 Error"
                    err_code = result.retcode if result else "N/A"
                    self.log_info(
                        f"❌ MT5 REJECTED {symbol}: {err_msg} "
                        f"(retcode: {err_code})"
                    )

            except Exception as e:
                self.log_info(f"⚠️ Thread Execution Error on {symbol}: {e}")
            finally:
                self.execution_lock.discard(symbol)

        threading.Thread(target=_async_execute, daemon=True).start()

    # ──────────────────────────────────────────────────────────
    # DASHBOARD API
    # ──────────────────────────────────────────────────────────
    def get_status(self) -> dict:
        acc     = self.gateway.get_account_info()
        raw_pos = self.gateway.get_open_positions()
        return {
            "is_running":    self.is_running,
            "watched_symbols": self.active_symbols,
            "recent_logs":   self.logs,
            "account":       acc,
            "positions":     raw_pos,
            "total_pnl":     sum(p["profit"] for p in raw_pos) if raw_pos else 0.0,
            "market_regime": self.market_regime,
            "daily_var":     self.current_var,
            "kill_switch":   self.kill_switch_active,
        }

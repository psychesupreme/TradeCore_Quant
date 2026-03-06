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
        # FULLY DIVERSIFIED ASSET MATRIX
        # ==========================================
        self.vip_assets = [
            "EURUSD", "GBPUSD", "USDJPY", "USDCAD", "USDCHF", "AUDUSD", "NZDUSD", 
            "EURJPY", "GBPJPY", "EURGBP", "AUDJPY",  
            "XAUUSD", "XAGUSD",                      
            "BTCUSD", "ETHUSD",                      
            "US SP 500", "US Tech 100"               
        ]
        
        self.active_symbols = [] 
        self.symbol_cooldowns = {} 
        
        # Expanded absolute capacity limits
        self.MAX_OPEN_TRADES = 12       
        self.MAX_SNIPER_SLOTS = 5      
        self.MAX_GOLD_TRADES = 3       
        
        self.logs = []
        self.is_running = False
        self.active_tickets = set()
        self.execution_lock = set() 
        
        # --- STATE PERSISTENCE ---
        self.state_file = "logs/tradecore_state.json"
        self.scaled_positions = self._load_state()
        
        self.daily_start_balance = 0.0
        self.last_trade_day = -1
        self.kill_switch_active = False

        self.current_var = 0.0
        self.market_regime = "CALIBRATING..."

        # [OPT-6] Signal deduplication tracker.
        # Prevents the same LOW_CONFIDENCE signal being written to DB every 60s.
        # Format: {symbol: (signal_type, confidence_rounded)}
        # A new DB row is written only when signal type OR confidence changes
        # by more than 2%. This reduced signal table noise from 50+ identical
        # rows/hour (EURUSD SELL_MICRO 83%) to one row per meaningful change.
        self._last_logged_signal: dict = {}

    def _load_state(self):
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
            f"📊 **TradeCore v51.0 Status**\n"
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
        self.log_info(f"✅ TradeCore v51.0: Engine Active. Monitoring {len(self.active_symbols)} Assets.")
        
        self.news_manager.fetch_calendar()
        self.notifier.start_listening(self.handle_telegram_command)
        self.async_alert("🚀 **TradeCore v51.0 Master Online**\nDynamic Structural Targets Armed.")
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
        elif "SP 500" in s or "Tech 100" in s:
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

    def evaluate_risk_metrics(self):
        """
        [OPT-5] Proper parametric VaR using ATR-based daily return std-dev.

        Old formula: raw_var = balance × 0.15 × 2.326 × (vol_pct × 100)
        Problems:
          - vol_pct is a price range not a return standard deviation
          - EURUSD-only proxy for a 17-asset portfolio
          - 5% floor meant VaR could never drop below $461 on $9k balance —
            the kill switch barely fires during calm sessions
          - 25% ceiling is appropriate ✅ (kept)

        New formula (proper parametric VaR):
          daily_return_std = ATR / close  (ATR ≈ 1-day price std-dev)
          99% daily VaR    = balance × daily_return_std × z(99%) × 2.326
          Portfolio VaR    = sqrt(N) scaling for N open positions
          Clamp            = [1.5% floor, 20% ceiling]  (tighter than before)

        The 1.5% floor means the kill switch fires if we lose >$138 on a
        $9k account during a quiet session — more protective than $461.
        """
        try:
            import MetaTrader5 as mt5
            df = self.gateway.get_market_data("EURUSD")
            if df.empty or len(df) < 15:
                return "CALIBRATING...", 0.0

            recent_high = df['high'].iloc[-15:].max()
            recent_low  = df['low'].iloc[-15:].min()
            current_price = df['close'].iloc[-1]
            vol_pct = (recent_high - recent_low) / current_price

            # Regime still based on EURUSD for the global engine state label
            # (per-asset regime used inside process_symbol via get_asset_regime)
            if vol_pct > 0.008:
                regime = "HIGH VOLATILITY (GARCH)"
            elif vol_pct > 0.003:
                regime = "NORMAL (TRENDING)"
            else:
                regime = "DEAD MARKET"

            acc     = self.gateway.get_account_info()
            balance = acc['balance'] if acc else 10000.0

            # ATR-based return std-dev: more statistically correct than raw range
            df['atr'] = calculate_atr(df, period=14)
            atr_val = df['atr'].iloc[-1]
            if current_price > 0 and atr_val > 0:
                daily_return_std = atr_val / current_price
            else:
                daily_return_std = vol_pct

            # Number of open positions for portfolio scaling
            open_pos = self.gateway.get_open_positions()
            n_pos    = max(1, len(open_pos))

            # Parametric 99% VaR — portfolio-scaled
            z_99         = 2.326
            single_var   = balance * daily_return_std * z_99
            portfolio_var = single_var * (n_pos ** 0.5)   # sqrt(N) for uncorrelated positions

            # Clamp: [1.5%, 20%] — tighter floor than old 5%, same ceiling direction
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
        
        DBManager.log_snapshot(acc['balance'], acc['equity'], acc['margin_level'], acc['free_margin'])
        current_positions = self.gateway.get_open_positions()
        
        current_day = datetime.utcnow().day
        
        if self.kill_switch_active:
            if current_day != self.last_trade_day:
                self.log_info("🌅 Midnight UTC Reached. Resetting Daily Kill-Switch.")
                self.kill_switch_active = False
                self.daily_start_balance = acc['balance']
                self.last_trade_day = current_day
            else:
                if datetime.now().minute % 30 == 0 and datetime.now().second < 5:
                    self.log_info("🛑 Kill Switch Active. Waiting for Midnight UTC to resume.")
                return 

        if current_day != self.last_trade_day:
            self.daily_start_balance = acc['balance']
            self.last_trade_day = current_day

        self.market_regime, self.current_var = self.evaluate_risk_metrics()

        if self.daily_start_balance > 0 and self.current_var > 0:
            current_dd_usd = self.daily_start_balance - acc['equity']
            
            if current_dd_usd >= self.current_var: 
                self.log_info(f"🛑 KILL SWITCH: 99% VaR Limit Breached! (Drawdown: ${current_dd_usd:.2f} | Limit: ${self.current_var:.2f})")
                self.async_alert(f"🛑 **CRITICAL: VALUE AT RISK (VaR) BREACHED**\nAccount hit the dynamic volatility limit (${self.current_var:.2f}). Liquidating {len(current_positions)} positions.")
                self.close_all_positions(current_positions)
                self.kill_switch_active = True
                return

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
                if time_since_close < timedelta(minutes=60):
                    continue 
            
            # Dynamic USD Exposure Lock
            current_usd_locks = len([s for s in self.execution_lock if "USD" in s])
            if "USD" in symbol and (usd_exposure_base + current_usd_locks) >= 2:
                continue

            symbol_trades = len([p for p in current_positions if p['symbol'] == symbol]) + (1 if symbol in self.execution_lock else 0)
            
            nano_trades = len([p for p in current_positions if p['symbol'] == symbol and p.get('magic', 510000) == 510001])
            if "DEAD MARKET" in self.market_regime and nano_trades >= 1:
                continue 

            if "XAU" in symbol or "XAG" in symbol:
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
                
                scale_key = f"{symbol}_{open_price}_{pos['type']}"
                is_ready_to_scale = False
                
                if ("XAU" in symbol or "XAG" in symbol) and profit_dist > 2.0: 
                    is_ready_to_scale = True
                elif "JPY" in symbol and profit_dist > 0.200: 
                    is_ready_to_scale = True
                elif ("BTC" in symbol or "ETH" in symbol or "US SP 500" in symbol or "US Tech 100" in symbol) and profit_dist > 50.0: 
                    is_ready_to_scale = True
                elif "XAU" not in symbol and "XAG" not in symbol and "JPY" not in symbol and "BTC" not in symbol and "ETH" not in symbol and "US SP 500" not in symbol and "US Tech 100" not in symbol and profit_dist > 0.0020: 
                    is_ready_to_scale = True

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
                    if "XAU" in symbol or "XAG" in symbol:
                        if profit_dist > 5.0:       
                            lock_price = open_price + (profit_dist * 0.70) if is_buy else open_price - (profit_dist * 0.70)
                        elif profit_dist > 2.0:     
                            lock_price = open_price + (profit_dist * 0.50) if is_buy else open_price - (profit_dist * 0.50)
                            
                    elif "BTC" in symbol or "ETH" in symbol or "US SP 500" in symbol or "US Tech 100" in symbol:
                        if profit_dist > 100.0:
                            lock_price = open_price + (profit_dist * 0.80) if is_buy else open_price - (profit_dist * 0.80)
                        elif profit_dist > 50.0:
                            lock_price = open_price + (profit_dist * 0.50) if is_buy else open_price - (profit_dist * 0.50)
                        
                    elif "JPY" in symbol:
                        if profit_dist > 0.400:    
                            lock_price = open_price + (profit_dist * 0.75) if is_buy else open_price - (profit_dist * 0.75)
                        elif profit_dist > 0.200:  
                            lock_price = open_price + (profit_dist * 0.50) if is_buy else open_price - (profit_dist * 0.50)
                        
                    else: 
                        if profit_dist > 0.0040:    
                            lock_price = open_price + (profit_dist * 0.80) if is_buy else open_price - (profit_dist * 0.80)
                        elif profit_dist > 0.0020:  
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
        elif "US SP 500" in symbol or "US Tech 100" in symbol:
            limit = 5000
        elif "XAU" in symbol or "XAG" in symbol:
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
            required_conf = 0.92 if is_sniper_mode else 0.88

            if analysis.signal != "NEUTRAL":
                 is_nano = "NANO" in analysis.signal
                 if is_nano and any(x in symbol for x in ["XAU", "XAG", "BTC", "ETH", "US SP 500", "US Tech 100"]):
                     self.log_debug(f"[{symbol}] NANO LOCK: Skipped (Spread drag too high).")
                     return

                 if analysis.confidence >= required_conf:
                     if is_sniper_mode:
                         self.log_info(f"🎯 GLOBAL SNIPER OVERRIDE: {symbol} {analysis.signal} (Conf: {analysis.confidence*100:.0f}%)")
                     else:
                         self.log_info(f"🔎 MTF Confluence Locked: {symbol} {analysis.signal} (Conf: {analysis.confidence*100:.0f}%)")
                     
                     result_status = "ATTEMPTED"
                     self.execute_signal(symbol, analysis, df_micro, props) 
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
                safe_reason = getattr(analysis, 'reason', 'No reason provided')
                indicators  = {"trend": "MTF_Managed", "reason": safe_reason,
                               "regime": symbol_regime}
                DBManager.log_signal(symbol, analysis.signal, analysis.confidence,
                                     indicators, result_status)
                self._last_logged_signal[symbol] = (analysis.signal, conf_bucket, result_status)

        except Exception as e: 
            self.log_debug(f"Process Error on {symbol}: {e}")

    def execute_signal(self, symbol, analysis, df, props):
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
                
                min_buffer = 10.0 if "BTC" in symbol else 2.0 if "ETH" in symbol else 0.50 if "XAU" in symbol else 0.10 if "JPY" in symbol else 0.0010
                volatility_buffer = max(structure_range, min_buffer)

                magic_number = 510001 if is_nano else 510000

                # ==========================================
                # BOUNDED DYNAMIC NANO STOP LOSS
                # ==========================================
                base_nano_sl = volatility_buffer * 0.4
                floor_sl = 0.060 if "JPY" in symbol else 0.00060  
                ceil_sl = 0.150 if "JPY" in symbol else 0.00150   
                
                dynamic_nano_sl = max(floor_sl, min(base_nano_sl, ceil_sl))
                # [OPT-3] NANO TP ratio upgraded from 1.5 to 2.0.
                # At 60% win rate, 1.5:1 barely breaks even (EV = 0.60).
                # At 2.0:1 and current 71% WR: EV = 0.71×2.0 − 0.29 = +1.13.
                # Even at 50% WR (worst case), 2.0:1 gives EV = +0.5 vs 0.0 at 1.5:1.
                dynamic_nano_tp = dynamic_nano_sl * 2.0

                if is_buy:
                    if is_nano:
                        action = "BUY_MARKET"
                        raw_price = tick.ask
                        sl_price = tick.bid - dynamic_nano_sl
                        tp_price = tick.ask + dynamic_nano_tp
                    else:
                        action = "BUY_LIMIT"
                        raw_price = df.iloc[-1]['low']
                        sl_price = df.iloc[-3]['low'] - (volatility_buffer * 0.1)
                        tp_price = local_high + (volatility_buffer * 0.2)
                else:
                    if is_nano:
                        action = "SELL_MARKET"
                        raw_price = tick.bid
                        sl_price = tick.ask + dynamic_nano_sl
                        tp_price = tick.bid - dynamic_nano_tp
                    else:
                        action = "SELL_LIMIT"
                        raw_price = df.iloc[-1]['high']
                        sl_price = df.iloc[-3]['high'] + (volatility_buffer * 0.1)
                        tp_price = local_low - (volatility_buffer * 0.2)
                    
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
                        stops_pt = sym_info.stops_level * sym_info.point
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
                
                risk_multiplier = 0.5 if (margin_level > 0.0 and margin_level < 500.0) else 1.0 
                if is_nano:
                    risk_multiplier = risk_multiplier * 0.25 

                if "XAU" in symbol or "XAG" in symbol:
                    risk_capital = (balance * 0.01) * risk_multiplier
                    capital_per_lot = sl_distance * 100
                    min_lot = 0.20
                elif "JPY" in symbol:
                    risk_capital = (balance * 0.02) * risk_multiplier 
                    capital_per_lot = sl_distance * 1000 
                    min_lot = 0.30
                else:
                    risk_capital = (balance * 0.02) * risk_multiplier 
                    capital_per_lot = sl_distance * 100000
                    min_lot = 0.30
                    
                calculated_lot = round(risk_capital / capital_per_lot, 2)
                lot = max(min_lot, calculated_lot)
                
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
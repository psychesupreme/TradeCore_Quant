import os
import logging
import json
import matplotlib
matplotlib.use('Agg') 
import matplotlib.pyplot as plt 

import pandas as pd
from datetime import datetime, timedelta
import time
import MetaTrader5 as mt5 
from mt5_interface import MT5Gateway
from analyst import analyze_market_structure, AnalysisRequest
from models import Candle
from telegram_client import TelegramNotifier
from db_manager import DBManager 
from news_manager import NewsManager  
from vision_module import VisionEngine 
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
        
        self.vip_assets = [
            "EURUSD", "GBPUSD", "USDJPY", 
            "USDCAD", "USDCHF", "AUDUSD", "NZDUSD", 
            "XAUUSD"
        ]
        
        self.active_symbols = [] 
        self.symbol_cooldowns = {} 
        
        self.MAX_OPEN_TRADES = 7       
        self.MAX_SNIPER_SLOTS = 5      
        self.MAX_GOLD_TRADES = 3       
        
        self.logs = []
        self.is_running = False
        self.active_tickets = set()
        self.execution_lock = set() 
        
        # --- STATE PERSISTENCE: Load Memory on Boot ---
        self.state_file = "logs/tradecore_state.json"
        self.scaled_positions = self._load_state()
        
        self.daily_start_balance = 0.0
        self.last_trade_day = -1
        self.kill_switch_active = False

        self.current_var = 0.0
        self.market_regime = "CALIBRATING..."

    # ==========================================
    # PERSISTENT MEMORY MANAGEMENT
    # ==========================================
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
        if len(self.logs) > 100: self.logs.pop()

    def log_debug(self, message):
        logger.debug(message)

    def async_alert(self, msg):
        def _send():
            try: self.notifier.send(msg)
            except: pass
        threading.Thread(target=_send).start()

    def handle_telegram_command(self, command):
        cmd = command.split()[0].lower()
        self.log_info(f"📩 Received Command: {cmd}")
        
        if cmd == "/status": self._report_status()
        elif cmd == "/news":
            news_data = self.news_manager.get_upcoming_news()
            if not news_data: self.async_alert("🌍 **No High Impact News Found.**")
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
            if not self.is_running: self.start_service()
        elif cmd == "/balance":
            acc = self.gateway.get_account_info()
            if acc: self.async_alert(f"💰 **Balance:** ${acc['balance']:.2f}\n**Equity:** ${acc['equity']:.2f}")

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

        if day == 4 and (hour > 21 or (hour == 21 and minute >= 50)): return False, "Weekend Close Phase"
        if day == 5: return False, "Weekend Closed"
        if day == 6 and (hour < 22 or (hour == 22 and minute <= 5)): return False, "Sunday Open Phase"
        if (hour == 21 and minute >= 50) or (hour == 22 and minute <= 10): return False, "Daily Rollover (Danger Zone)"

        return True, "Market Open"

    def close_all_positions(self, positions):
        for pos in positions:
            self.gateway.close_position(pos['ticket'], pos['symbol'], pos['volume'], pos['type'])

    def evaluate_open_positions(self, positions):
        for pos in positions:
            try:
                symbol = pos['symbol']
                is_buy = pos['type'] == 'BUY'
                
                df = self.gateway.get_market_data(symbol, timeframe=mt5.TIMEFRAME_M15)
                if df.empty: continue
                
                candles = [Candle(**row) for row in df.to_dict('records') if hasattr(row['time'], 'year')]
                req = AnalysisRequest(symbol=symbol, candles=candles, daily_trend="NEUTRAL")
                analysis = analyze_market_structure(req, None)
                
                if analysis.signal != "NEUTRAL" and analysis.confidence >= 0.90:
                    if (is_buy and "SELL" in analysis.signal) or (not is_buy and "BUY" in analysis.signal):
                        self.log_info(f"🔄 DYNAMIC INVALIDATION: Market reversed on {symbol}. Closing early to protect margin.")
                        self.gateway.close_position(pos['ticket'], symbol, pos['volume'], pos['type'])
                        self.async_alert(f"🔄 **Trade Scratched Early:** {symbol} structure collapsed. Capital reclaimed.")
                        
                        self.symbol_cooldowns[symbol] = datetime.now()
            except Exception:
                pass

    def evaluate_risk_metrics(self):
        try:
            df = self.gateway.get_market_data("EURUSD")
            if df.empty or len(df) < 15:
                return "CALIBRATING...", 0.0

            recent_high = df['high'].iloc[-15:].max()
            recent_low = df['low'].iloc[-15:].min()
            current_price = df['close'].iloc[-1]
            
            vol_pct = (recent_high - recent_low) / current_price

            if vol_pct > 0.008: 
                regime = "HIGH VOLATILITY (GARCH)"
            elif vol_pct > 0.003: 
                regime = "NORMAL (TRENDING)"
            else: 
                regime = "LOW VOLATILITY (CHOP)"

            acc = self.gateway.get_account_info()
            balance = acc['balance'] if acc else 10000.0
            
            raw_var = balance * 0.10 * 2.326 * (vol_pct * 100)
            daily_var_usd = max(balance * 0.03, min(raw_var, balance * 0.12))
            
            return regime, round(daily_var_usd, 2)

        except Exception as e:
            self.log_debug(f"⚠️ VaR Calculation Error: {e}")
            return "UNKNOWN", 0.0

    def run_cycle(self):
        if not self.is_running: return
        
        acc = self.gateway.get_account_info()
        if not acc: return
        
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
                self.async_alert(f"🛑 **CRITICAL: VALUE AT RISK (VaR) BREACHED**\nAccount hit the dynamic volatility limit (${self.current_var:.2f}). Liquidating {len(current_positions)} positions and locking system until midnight.")
                self.close_all_positions(current_positions)
                self.kill_switch_active = True
                return

        is_open, market_status = self.check_market_schedule()
        if not is_open:
            if datetime.now().minute % 30 == 0 and datetime.now().second < 5:
                self.log_info(f"💤 Market Offline: {market_status}. Bot standing by.")
            return 

        self.apply_trailing_stop(current_positions)
        self.evaluate_open_positions(current_positions) 
        self.active_tickets = {p['symbol'] for p in current_positions}

        gold_trades = len([p for p in current_positions if "XAU" in p['symbol']])
        current_count = len(current_positions) + len(self.execution_lock) 
        
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
                time_since_close = datetime.now() - self.symbol_cooldowns[symbol]
                if time_since_close < timedelta(minutes=60):
                    continue 

            symbol_trades = len([p for p in current_positions if p['symbol'] == symbol]) + (1 if symbol in self.execution_lock else 0)

            if "XAU" in symbol:
                if is_sniper_mode and gold_trades >= (self.MAX_GOLD_TRADES + 1): continue 
                elif not is_sniper_mode and gold_trades >= self.MAX_GOLD_TRADES: continue 
            
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
                if 'open_price' not in pos: continue

                tick = mt5.symbol_info_tick(symbol)
                if not tick: continue
                
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
                
                # ==========================================
                # NEW: INSTITUTIONAL SCALE-OUT LOGIC (1:1 RR)
                # ==========================================
                scale_key = f"{symbol}_{open_price}_{pos['type']}"
                is_ready_to_scale = False
                
                if "XAU" in symbol and profit_dist > 2.0: is_ready_to_scale = True
                elif "JPY" in symbol and profit_dist > 0.200: is_ready_to_scale = True
                elif "XAU" not in symbol and "JPY" not in symbol and profit_dist > 0.0020: is_ready_to_scale = True

                if is_ready_to_scale and scale_key not in self.scaled_positions:
                    half_vol = current_vol / 2.0
                    close_vol = round(half_vol / vol_step) * vol_step
                    
                    if close_vol >= min_lot:
                        self.log_info(f"⚖️ Scaling Out: {symbol} hit 1:1 RR. Closing {close_vol} Lots to secure cash.")
                        success = self.gateway.close_position(ticket, symbol, close_vol, pos['type'])
                        
                        if success:
                            # SAVE STATE TO DISK
                            self.scaled_positions.add(scale_key)
                            self._save_state()
                            
                            self.async_alert(f"⚖️ **Partial Take Profit:** {symbol}\nSecured 50% Volume. Moving SL to Breakeven.")
                            
                            breakeven_buffer = props['point'] * 5 
                            lock_price = open_price + breakeven_buffer if is_buy else open_price - breakeven_buffer
                    else:
                        self.scaled_positions.add(scale_key)
                        self._save_state()

                # ==========================================
                # STANDARD TRAILING LOGIC FOR THE RUNNER
                # ==========================================
                if "XAU" in symbol:
                    if profit_dist > 5.0:       
                        secured_dist = profit_dist * 0.70 
                        target = open_price + secured_dist if is_buy else open_price - secured_dist
                        lock_price = target if lock_price == 0 else target
                    elif profit_dist > 2.0:     
                        secured_dist = profit_dist * 0.50 
                        target = open_price + secured_dist if is_buy else open_price - secured_dist
                        lock_price = target if lock_price == 0 else target
                    
                elif "JPY" in symbol:
                    if profit_dist > 0.400:    
                        secured_dist = profit_dist * 0.75 
                        target = open_price + secured_dist if is_buy else open_price - secured_dist
                        lock_price = target if lock_price == 0 else target
                    elif profit_dist > 0.200:  
                        secured_dist = profit_dist * 0.50 
                        target = open_price + secured_dist if is_buy else open_price - secured_dist
                        lock_price = target if lock_price == 0 else target
                    
                else: 
                    if profit_dist > 0.0040:    
                        secured_dist = profit_dist * 0.80 
                        target = open_price + secured_dist if is_buy else open_price - secured_dist
                        lock_price = target if lock_price == 0 else target
                    elif profit_dist > 0.0020:  
                        secured_dist = profit_dist * 0.50 
                        target = open_price + secured_dist if is_buy else open_price - secured_dist
                        lock_price = target if lock_price == 0 else target
                
                if lock_price == 0: continue
                
                if is_buy:
                    max_allowed_sl = price_current - min_stop_dist
                    if lock_price > max_allowed_sl: lock_price = max_allowed_sl
                else:
                    min_allowed_sl = price_current + min_stop_dist
                    if lock_price < min_allowed_sl: lock_price = min_allowed_sl

                should_modify = False
                if current_sl == 0: should_modify = True
                elif is_buy and lock_price > current_sl: should_modify = True
                elif not is_buy and lock_price < current_sl: should_modify = True
                    
                if should_modify:
                    lock_price = self.gateway.normalize_price(symbol, lock_price) 
                    
                    req = {
                        "action": mt5.TRADE_ACTION_SLTP, "position": ticket,
                        "sl": lock_price, "tp": pos.get('tp', 0.0)
                    }
                    res = mt5.order_send(req)
                    if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                        self.log_info(f"🛡️ Dynamic Profit Locked: {symbol} SL secured at {lock_price}")
            except Exception:
                pass

    def process_symbol(self, symbol, is_sniper_mode=False, upcoming_news=None):
        now = datetime.now()
        if upcoming_news is None: upcoming_news = []
        
        for event in upcoming_news:
            if event.get('impact') == 'High':
                try:
                    event_time = datetime.strptime(event['time'].strip().lower(), "%m-%d-%Y %I:%M%p")
                    time_diff = event_time - now
                    if timedelta(minutes=-15) <= time_diff <= timedelta(minutes=15):
                        if datetime.now().second < 5:
                            self.log_info(f"📰 News Guard Active: Blocking {symbol} due to High Impact Event.")
                        return
                except ValueError: pass 

        if symbol in self.active_tickets or symbol in self.execution_lock: return 

        props = self.gateway.get_symbol_properties(symbol)
        if not props: return
        
        spread = (props['ask'] - props['bid']) / props['point']
        limit = 1000 if "XAU" in symbol else 60
        if spread > limit: return 
        
        pending_orders = mt5.orders_get(symbol=symbol)
        if pending_orders and len(pending_orders) > 0:
            return 

        df_micro = self.gateway.get_market_data(symbol, timeframe=mt5.TIMEFRAME_M15)
        df_macro = self.gateway.get_market_data(symbol, timeframe=mt5.TIMEFRAME_H4)
        
        if df_micro.empty or df_macro.empty: return

        try:
            candles_micro = [Candle(**row) for row in df_micro.to_dict('records') if hasattr(row['time'], 'year')]
            req = AnalysisRequest(symbol=symbol, candles=candles_micro, daily_trend="NEUTRAL")
            
            analysis = analyze_market_structure(req, df_macro=df_macro)
            
            result_status = "SKIPPED"
            required_conf = 0.92 if is_sniper_mode else 0.88

            if analysis.signal != "NEUTRAL":
                 if analysis.confidence >= required_conf:
                     if is_sniper_mode:
                         self.log_info(f"🎯 GLOBAL SNIPER OVERRIDE: {symbol} {analysis.signal} (Conf: {analysis.confidence*100:.0f}%)")
                     else:
                         self.log_info(f"🔎 MTF Confluence Locked: {symbol} {analysis.signal} (Conf: {analysis.confidence*100:.0f}%)")
                     
                     result_status = "EXECUTED"
                     self.execute_signal(symbol, analysis, df_micro)
                 else:
                     result_status = f"LOW_CONFIDENCE ({analysis.confidence*100:.0f}%)"
                     self.log_debug(f"[{symbol}] {analysis.reason}")
            else:
                 self.log_debug(f"[{symbol}] {analysis.reason}")
                 
            # Safely extract the reason without calling the removed 'trend' attribute
            safe_reason = getattr(analysis, 'reason', 'No reason provided')
            indicators = {"trend": "MTF_Managed", "reason": safe_reason}
            
            DBManager.log_signal(symbol, analysis.signal, analysis.confidence, indicators, result_status)
        except Exception as e: 
            self.log_debug(f"Process Error on {symbol}: {e}")

    def execute_signal(self, symbol, analysis, df):
        if symbol in self.execution_lock: return
        self.execution_lock.add(symbol)
        
        def _async_execute():
            try:
                is_buy = "BUY" in analysis.signal
                
                tick = mt5.symbol_info_tick(symbol)
                if not tick: return
                    
                live_ask = tick.ask
                live_bid = tick.bid
                
                c1 = df.iloc[-3]
                c3 = df.iloc[-1]
                
                recent_data = df.tail(15)
                local_high = recent_data['high'].max()
                local_low = recent_data['low'].min()
                structure_range = local_high - local_low
                min_buffer = 0.50 if "XAU" in symbol else (0.10 if "JPY" in symbol else 0.0010)
                volatility_buffer = max(structure_range, min_buffer)

                if is_buy:
                    action = "BUY_LIMIT"
                    raw_price = c3['low']
                    sl_price = c1['low'] - (volatility_buffer * 0.1) 
                    tp_price = local_high + (volatility_buffer * 0.2) 
                else:
                    action = "SELL_LIMIT"
                    raw_price = c3['high']
                    sl_price = c1['high'] + (volatility_buffer * 0.1)
                    tp_price = local_low - (volatility_buffer * 0.2)
                    
                price = self.gateway.normalize_price(symbol, raw_price)
                sl = self.gateway.normalize_price(symbol, sl_price) 
                tp = self.gateway.normalize_price(symbol, tp_price) 
                
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

                if "XAU" in symbol:
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
                
                expiration_time = int(time.time()) + (4 * 3600)
                
                request = {
                    "action": mt5.TRADE_ACTION_PENDING,
                    "symbol": symbol,
                    "volume": float(lot),
                    "price": float(price),
                    "sl": float(sl),
                    "tp": float(tp),
                    "deviation": 10,
                    "magic": 510000,
                    "comment": "SMC_Limit",
                    "type_time": mt5.ORDER_TIME_SPECIFIED,
                    "expiration": expiration_time,
                    "type_filling": mt5.ORDER_FILLING_IOC,
                }

                if is_buy:
                    if live_ask <= price:
                        request["action"] = mt5.TRADE_ACTION_DEAL
                        request["type"] = mt5.ORDER_TYPE_BUY
                        request["price"] = live_ask
                        del request["expiration"]
                        request["type_time"] = mt5.ORDER_TIME_GTC
                        action = "BUY"
                    else:
                        request["type"] = mt5.ORDER_TYPE_BUY_LIMIT
                else:
                    if live_bid >= price:
                        request["action"] = mt5.TRADE_ACTION_DEAL
                        request["type"] = mt5.ORDER_TYPE_SELL
                        request["price"] = live_bid
                        del request["expiration"]
                        request["type_time"] = mt5.ORDER_TIME_GTC
                        action = "SELL"
                    else:
                        request["type"] = mt5.ORDER_TYPE_SELL_LIMIT

                result = mt5.order_send(request)
                
                safe_action = action.replace("_", " ")

                if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                    self.log_info(f"🕸️ SMC TRAP SET: {symbol} {action} | Limit: {price} | Lot: {lot}")
                    self.async_alert(f"🕸️ **SMC Limit Trap Set**: {symbol} {safe_action}\nTarget Entry: {price}\nLot: {lot}\nConf: {analysis.confidence*100:.0f}%")
                    
                    try:
                        photo_path = VisionEngine.generate_trade_snapshot(df, symbol, action, price, sl, tp, analysis.confidence)
                        if photo_path:
                            caption = f"🎯 **SMC Snapshot:** {symbol} {safe_action}\nPending Entry: {price}\nSL: {sl} | TP: {tp}"
                            self.notifier.send_photo(photo_path, caption)
                            
                            def _delayed_cleanup(p):
                                time.sleep(5)
                                try: VisionEngine.cleanup_snapshot(p)
                                except: pass
                            threading.Thread(target=_delayed_cleanup, args=(photo_path,)).start()
                            
                    except Exception as e:
                        self.log_debug(f"⚠️ Vision Module failed to generate chart: {e}")
                        
                else:
                    err_msg = result.comment if result else "Unknown MT5 Error"
                    self.log_info(f"❌ MT5 REJECTED PENDING {symbol}: {err_msg}")

            except Exception as e:
                self.log_info(f"⚠️ Thread Execution Error on {symbol}: {e}")
            finally:
                self.execution_lock.discard(symbol)
                
        threading.Thread(target=_async_execute).start()
            
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
            "daily_var": self.current_var
        }
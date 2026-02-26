import matplotlib
matplotlib.use('Agg') 
import matplotlib.pyplot as plt 

import pandas as pd
from datetime import datetime, timedelta
import MetaTrader5 as mt5 
from mt5_interface import MT5Gateway
from analyst import analyze_market_structure, AnalysisRequest
from models import Candle
from telegram_client import TelegramNotifier
from db_manager import DBManager 
from news_manager import NewsManager  
from vision_module import VisionEngine 
import threading

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
        
        # --- CAPACITY CONFIGURATION ---
        self.MAX_OPEN_TRADES = 7       # Expanded to 7 normal slots
        self.MAX_SNIPER_SLOTS = 5      # Expanded to 5 Global Sniper slots (Total 12)
        self.MAX_GOLD_TRADES = 3       # Increased to allow XAU volume
        
        self.logs = []
        self.is_running = False
        self.active_tickets = set()
        self.execution_lock = set() # Prevents Ghost Order Cascades
        
        self.daily_start_balance = 0.0
        self.last_trade_day = -1
        self.kill_switch_active = False

    def log(self, message):
        timestamp = datetime.now().strftime("%H:%M:%S")
        entry = f"[{timestamp}] {message}"
        print(entry)
        self.logs.insert(0, entry)
        if len(self.logs) > 100: self.logs.pop()

    def async_alert(self, msg):
        def _send():
            try: self.notifier.send(msg)
            except: pass
        threading.Thread(target=_send).start()

    def handle_telegram_command(self, command):
        cmd = command.split()[0].lower()
        self.log(f"📩 Received Command: {cmd}")
        
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
        positions = self.gateway.get_open_positions()
        total_profit = sum(p['profit'] for p in positions)
        msg = f"📊 **System Status**\n**Active Trades:** {len(positions)}\n**Pending Executions:** {len(self.execution_lock)}\n**Total PnL:** ${total_profit:.2f}\n"
        for p in positions:
            icon = "🟢" if p['profit'] >= 0 else "🔴"
            msg += f"{icon} {p['symbol']} ({p['type']}): ${p['profit']:.2f}\n"
        self.async_alert(msg)

    def start_service(self):
        if not self.gateway.start():
            self.log("CRITICAL: MT5 Connection Failed")
            return False
        
        self.active_symbols = []
        for v in self.vip_assets:
            real = self.gateway.find_symbol(v)
            if real: 
                self.active_symbols.append(real)
                mt5.symbol_select(real, True)

        self.is_running = True
        self.execution_lock.clear()
        self.log(f"✅ TradeCore v51.0: Engine Active. Monitoring {len(self.active_symbols)} Assets.")
        
        self.news_manager.fetch_calendar()
        self.notifier.start_listening(self.handle_telegram_command)
        self.async_alert("🚀 **TradeCore v51.0 Master Online**\nAsync Execution & Dynamic Active Engine Armed.")
        return True

    def stop_service(self):
        self.is_running = False
        self.notifier.stop_listening()
        self.log("Stopped.")

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

    # --- ACTIVE EVALUATION ENGINE (Dynamic Invalidation) ---
    def evaluate_open_positions(self, positions):
        for pos in positions:
            try:
                symbol = pos['symbol']
                is_buy = pos['type'] == 'BUY'
                
                df = self.gateway.get_market_data(symbol)
                if df.empty: continue
                
                candles = [Candle(**row) for row in df.to_dict('records') if hasattr(row['time'], 'year')]
                req = AnalysisRequest(symbol=symbol, candles=candles, daily_trend="NEUTRAL")
                analysis = analyze_market_structure(req)
                
                # If market shifts heavily against the trade, cut the risk before SL is hit
                if analysis.signal != "NEUTRAL" and analysis.confidence >= 0.80:
                    if (is_buy and "SELL" in analysis.signal) or (not is_buy and "BUY" in analysis.signal):
                        self.log(f"🔄 DYNAMIC INVALIDATION: Market reversed on {symbol}. Closing early to protect margin.")
                        self.gateway.close_position(pos['ticket'], symbol, pos['volume'], pos['type'])
                        self.async_alert(f"🔄 **Trade Scratched Early:** {symbol} structure collapsed. Capital reclaimed.")
            except Exception:
                pass

    def run_cycle(self):
        if not self.is_running: return
        
        acc = self.gateway.get_account_info()
        if not acc: return
        
        DBManager.log_snapshot(acc['balance'], acc['equity'], acc['margin_level'], acc['free_margin'])
        current_positions = self.gateway.get_open_positions()
        
        current_day = datetime.utcnow().day
        
        if self.kill_switch_active:
            if current_day != self.last_trade_day:
                self.log("🌅 Midnight UTC Reached. Resetting Daily Kill-Switch.")
                self.kill_switch_active = False
                self.daily_start_balance = acc['balance']
                self.last_trade_day = current_day
            else:
                if datetime.now().minute % 30 == 0 and datetime.now().second < 5:
                    self.log("🛑 Kill Switch Active. Waiting for Midnight UTC to resume.")
                return 

        if current_day != self.last_trade_day:
            self.daily_start_balance = acc['balance']
            self.last_trade_day = current_day

        if self.daily_start_balance > 0:
            daily_dd_pct = (self.daily_start_balance - acc['equity']) / self.daily_start_balance
            if daily_dd_pct >= 0.04: 
                self.log(f"🛑 KILL SWITCH: 4% Daily Drawdown Hit! (Start: {self.daily_start_balance:.2f}, Equity: {acc['equity']:.2f})")
                self.async_alert(f"🛑 **CRITICAL: DAILY KILL SWITCH TRIGGERED**\nAccount hit 4% drawdown. Liquidating {len(current_positions)} positions and locking system until midnight.")
                self.close_all_positions(current_positions)
                self.kill_switch_active = True
                return

        is_open, market_status = self.check_market_schedule()
        if not is_open:
            if datetime.now().minute % 30 == 0 and datetime.now().second < 5:
                self.log(f"💤 Market Offline: {market_status}. Bot standing by.")
            return 

        self.apply_trailing_stop(current_positions)
        self.evaluate_open_positions(current_positions) 
        self.active_tickets = {p['symbol'] for p in current_positions}

        gold_trades = len([p for p in current_positions if "XAU" in p['symbol']])
        # Capacity Check: Active Trades + Pending Executions (The Ghost Shield)
        current_count = len(current_positions) + len(self.execution_lock) 
        
        if current_count >= (self.MAX_OPEN_TRADES + self.MAX_SNIPER_SLOTS):
            if datetime.now().second < 5: 
                self.log(f"⏸️ Absolute Capacity Full ({current_count}/{self.MAX_OPEN_TRADES + self.MAX_SNIPER_SLOTS}). System maxed out.")
            return

        # Sniper Mode Logic
        is_sniper_mode = (current_count >= self.MAX_OPEN_TRADES)
        if is_sniper_mode and datetime.now().second < 5:
            self.log(f"🎯 Base capacity full. 90%+ Global Sniper Mode Active ({self.MAX_OPEN_TRADES + self.MAX_SNIPER_SLOTS - current_count} slots left).")
        
        for symbol in self.active_symbols:
            # Count how many trades are currently active/pending for THIS specific symbol
            symbol_trades = len([p for p in current_positions if p['symbol'] == symbol]) + (1 if symbol in self.execution_lock else 0)

            if "XAU" in symbol:
                if is_sniper_mode and gold_trades >= (self.MAX_GOLD_TRADES + 1): continue 
                elif not is_sniper_mode and gold_trades >= self.MAX_GOLD_TRADES: continue 
            
            # --- NEW: PER-ASSET EXPOSURE CAP ---
            if is_sniper_mode and symbol_trades >= 3: 
                continue # Hard cap at 3 for 90%+ Sniper Setups
            elif not is_sniper_mode and symbol_trades >= 2: 
                continue # Hard cap at 2 for 85%+ Normal Setups
            
            self.process_symbol(symbol, is_sniper_mode)

    # --- DYNAMIC PERCENTAGE LOCK (Trailing Stop) ---
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

                price_current = tick.bid if pos['type'] == 'BUY' else tick.ask
                is_buy = pos['type'] == 'BUY'
                open_price = pos['open_price']
                current_sl = pos.get('sl', 0.0)
                
                profit_dist = (price_current - open_price) if is_buy else (open_price - price_current)
                lock_price = 0.0
                
                # Logic: XAU > 5.0 -> Lock 70%, XAU > 2.0 -> Lock 50%
                if "XAU" in symbol:
                    if profit_dist > 5.0:       
                        secured_dist = profit_dist * 0.70 
                        lock_price = open_price + secured_dist if is_buy else open_price - secured_dist
                    elif profit_dist > 2.0:     
                        secured_dist = profit_dist * 0.50 
                        lock_price = open_price + secured_dist if is_buy else open_price - secured_dist
                    else: continue
                    
                # Logic: JPY > 0.40 -> Lock 75%, JPY > 0.20 -> Lock 50%
                elif "JPY" in symbol:
                    if profit_dist > 0.400:    
                        secured_dist = profit_dist * 0.75 
                        lock_price = open_price + secured_dist if is_buy else open_price - secured_dist
                    elif profit_dist > 0.200:  
                        secured_dist = profit_dist * 0.50 
                        lock_price = open_price + secured_dist if is_buy else open_price - secured_dist
                    else: continue
                    
                # Logic: Forex > 40 pips -> Lock 80%, Forex > 20 pips -> Lock 50%
                else: 
                    if profit_dist > 0.0040:    
                        secured_dist = profit_dist * 0.80 
                        lock_price = open_price + secured_dist if is_buy else open_price - secured_dist
                    elif profit_dist > 0.0020:  
                        secured_dist = profit_dist * 0.50 
                        lock_price = open_price + secured_dist if is_buy else open_price - secured_dist
                    else: continue
                
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
                    lock_price = self.gateway.normalize_price(symbol, lock_price) # Precision Fix
                    
                    req = {
                        "action": mt5.TRADE_ACTION_SLTP, "position": ticket,
                        "sl": lock_price, "tp": pos.get('tp', 0.0)
                    }
                    res = mt5.order_send(req)
                    if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                        self.log(f"🛡️ Dynamic Profit Locked: {symbol} SL secured at {lock_price}")
            except Exception:
                pass

    def process_symbol(self, symbol, is_sniper_mode=False):
        now = datetime.now()
        upcoming_news = self.news_manager.get_upcoming_news()
        
        # News Guard
        for event in upcoming_news:
            if event['impact'] == 'High':
                try:
                    event_time = datetime.strptime(event['time'].strip().lower(), "%m-%d-%Y %I:%M%p")
                    time_diff = event_time - now
                    if timedelta(minutes=-15) <= time_diff <= timedelta(minutes=15):
                        self.log(f"📰 News Guard Active: Blocking {symbol} due to High Impact Event.")
                        return
                except ValueError: pass 

        if symbol in self.active_tickets or symbol in self.execution_lock: return 

        props = self.gateway.get_symbol_properties(symbol)
        if not props: return
        
        spread = (props['ask'] - props['bid']) / props['point']
        limit = 1000 if "XAU" in symbol else 60
        if spread > limit: return 

        df = self.gateway.get_market_data(symbol)
        if df.empty: return

        try:
            candles = [Candle(**row) for row in df.to_dict('records') if hasattr(row['time'], 'year')]
            req = AnalysisRequest(symbol=symbol, candles=candles, daily_trend="NEUTRAL")
            analysis = analyze_market_structure(req)
            
            result_status = "SKIPPED"
            
            # --- CONFIDENCE THRESHOLD LOGIC ---
            # 90% if Sniper Mode is active (Global)
            # 87% if Gold (Normal Mode) - LOWERED FROM 90%
            # 85% if Forex (Normal Mode)
            required_conf = 0.90 if is_sniper_mode else (0.87 if "XAU" in symbol else 0.85)

            if analysis.signal != "NEUTRAL":
                 if analysis.confidence >= required_conf:
                     if is_sniper_mode:
                         self.log(f"🎯 GLOBAL SNIPER OVERRIDE: {symbol} {analysis.signal} (Conf: {analysis.confidence*100:.0f}%) using emergency slot!")
                     else:
                         self.log(f"🔎 Ultra-Conviction Signal: {symbol} {analysis.signal} (Conf: {analysis.confidence*100:.0f}%)")
                     
                     result_status = "EXECUTED"
                     self.execute_signal(symbol, analysis, df)
                 else:
                     result_status = f"LOW_CONFIDENCE ({analysis.confidence*100:.0f}%)"
            
            indicators = {"trend": analysis.trend, "reason": analysis.reason}
            DBManager.log_signal(symbol, analysis.signal, analysis.confidence, indicators, result_status)
        except: pass

    # --- ASYNC EXECUTION THREAD (Fire & Forget) ---
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
                
                # SL/TP Distances
                if "XAU" in symbol:
                    sl_distance = 5.0
                    tp_distance = 10.0
                elif "JPY" in symbol:
                    sl_distance = 0.500   
                    tp_distance = 1.000
                else:
                    sl_distance = 0.0050  
                    tp_distance = 0.0100
                    
                if is_buy:
                    action = "BUY"
                    price = live_ask
                    sl_price = live_ask - sl_distance
                    tp_price = live_ask + tp_distance
                else:
                    action = "SELL"
                    price = live_bid
                    sl_price = live_bid + sl_distance
                    tp_price = live_bid - tp_distance
                    
                sl = self.gateway.normalize_price(symbol, sl_price) # Precision Fix
                tp = self.gateway.normalize_price(symbol, tp_price) # Precision Fix

                acc_info = self.gateway.get_account_info()
                balance = acc_info['balance'] if acc_info else 10000.0
                free_margin = acc_info['free_margin'] if acc_info else 10000.0
                
                if free_margin < (balance * 0.15):
                    self.log(f"⚠️ Margin Alert: Cannot open {symbol}. Free Margin too low (${free_margin:.2f})")
                    return
                
                # --- FRACTIONAL KELLY SIZING ---
                if "XAU" in symbol:
                    risk_capital = balance * 0.01 # 1% for Gold (Lower Edge)
                    capital_per_lot = sl_distance * 100
                    min_lot = 0.20
                elif "JPY" in symbol:
                    risk_capital = balance * 0.02 # 2% for JPY (High Edge)
                    capital_per_lot = sl_distance * 1000 
                    min_lot = 0.30
                else:
                    risk_capital = balance * 0.02 # 2% for Forex (High Edge)
                    capital_per_lot = sl_distance * 100000
                    min_lot = 0.30
                    
                calculated_lot = round(risk_capital / capital_per_lot, 2)
                lot = max(min_lot, calculated_lot)
                
                res = self.gateway.execute_trade(symbol, action, lot, sl, tp)
                
                if res and res["success"]:
                    self.log(f"🚀 EXECUTE CONFIRMED: {symbol} | Lot: {lot}")
                    self.async_alert(f"🚀 **TradeCore Executed**: {symbol} {action}\nLot: {lot}\nConf: {analysis.confidence*100:.0f}%")
                    ticket = res.get('ticket', 0)
                    try: DBManager.save_trade(ticket, symbol, action, lot, price, sl, tp, datetime.now())
                    except: pass
                    
                    try:
                        photo_path = VisionEngine.generate_trade_snapshot(df, symbol, action, price, sl, tp, analysis.confidence)
                        if photo_path:
                            caption = f"🎯 **Trade Snapshot:** {symbol} {action}\nEntry: {price}\nSL: {sl} | TP: {tp}"
                            self.notifier.send_photo(photo_path, caption)
                            VisionEngine.cleanup_snapshot(photo_path)
                    except Exception as e:
                        self.log(f"⚠️ Vision Module failed to generate chart: {e}")
                        
                elif res:
                    self.log(f"❌ BROKER REJECTED {symbol}: {res['message']}")
            except Exception as e:
                self.log(f"⚠️ Thread Execution Error on {symbol}: {e}")
            finally:
                self.execution_lock.discard(symbol)
                
        # Launch Async Thread
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
            "total_pnl": sum(p['profit'] for p in raw_pos) if raw_pos else 0.0
        }
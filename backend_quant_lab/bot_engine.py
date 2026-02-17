import pandas as pd
from datetime import datetime, time
import MetaTrader5 as mt5 
from mt5_interface import MT5Gateway
from analyst import analyze_market_structure, AnalysisRequest
from models import Candle
from telegram_client import TelegramNotifier
from db_manager import DBManager 
import threading

class TradingBot:
    def __init__(self):
        self.gateway = MT5Gateway()
        self.notifier = TelegramNotifier() 
        
        self.vip_assets = [
            "EURUSD", "GBPUSD", "USDJPY", 
            "USDCAD", "USDCHF", "AUDUSD", "NZDUSD", 
            "XAUUSD"
        ]
        
        self.active_symbols = [] 
        self.MAX_OPEN_TRADES = 3
        
        # Trailing Stop Configuration
        self.TRAIL_TRIGGER = 200  # Points (20 pips)
        
        self.logs = []
        self.is_running = False
        self.active_tickets = set()

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
            else:
                self.log(f"⚠️ Warning: Broker missing {v}")

        self.is_running = True
        self.log(f"✅ TradeCore v49.1: Trailing Fix. Monitoring {len(self.active_symbols)} Assets.")
        self.async_alert("🛠️ **System Patched & Online**")
        return True

    def stop_service(self):
        self.is_running = False
        self.log("Stopped.")

    def run_cycle(self):
        if not self.is_running: return
        
        # 1. Health
        acc = self.gateway.get_account_info()
        if acc:
            DBManager.log_snapshot(acc['balance'], acc['equity'], acc['margin_level'], acc['free_margin'])
            if acc['margin_level'] != 0 and acc['margin_level'] < 150: 
                self.log("⚠️ LOW MARGIN. Pausing.")
                return 
            
        current_positions = self.gateway.get_open_positions()
        
        # 2. Logic: Trailing Stop
        self.apply_trailing_stop(current_positions)
        
        self.active_tickets = {p['symbol'] for p in current_positions}

        # 3. Entry
        if len(current_positions) >= self.MAX_OPEN_TRADES:
            if datetime.now().second < 5: 
                self.log(f"⏸️ Max Trades Reached ({len(current_positions)}/{self.MAX_OPEN_TRADES}).")
            return
        
        if datetime.now().second < 5:
            self.log(f"--- Scanning {len(self.active_symbols)} Specialist Markets ---")
        
        for symbol in self.active_symbols:
            self.process_symbol(symbol)

    def check_trading_hours(self, symbol):
        now = datetime.utcnow().time()
        is_london = time(7,0) <= now <= time(16,0)
        is_ny = time(12,0) <= now <= time(21,0)
        is_tokyo = time(0,0) <= now <= time(9,0)
        
        if "JPY" in symbol: return is_tokyo or is_london or is_ny 
        if "EUR" in symbol or "GBP" in symbol: return is_london or is_ny
        if "USD" in symbol or "CAD" in symbol or "XAU" in symbol: return is_london or is_ny
        return True

    def apply_trailing_stop(self, positions):
        for pos in positions:
            try:
                symbol = pos['symbol']
                ticket = pos['ticket']
                
                # Check for required keys to avoid crashes
                if 'open_price' not in pos: continue

                tick = mt5.symbol_info_tick(symbol)
                if not tick: continue
                
                price_current = tick.bid if pos['type'] == 'BUY' else tick.ask
                point = mt5.symbol_info(symbol).point
                
                # Calculate Profit in Points
                if pos['type'] == 'BUY':
                    profit_points = (price_current - pos['open_price']) / point
                else:
                    profit_points = (pos['open_price'] - price_current) / point
                
                # Logic: If profit > Trigger, Move SL to Breakeven + 10 points
                if profit_points > self.TRAIL_TRIGGER:
                    new_sl = pos['open_price'] + (10 * point) if pos['type'] == 'BUY' else pos['open_price'] - (10 * point)
                    
                    should_modify = False
                    current_sl = pos.get('sl', 0.0)
                    
                    if current_sl == 0: should_modify = True
                    elif pos['type'] == 'BUY' and new_sl > current_sl: should_modify = True
                    elif pos['type'] == 'SELL' and new_sl < current_sl: should_modify = True
                    
                    if should_modify:
                        req = {
                            "action": mt5.TRADE_ACTION_SLTP,
                            "position": ticket,
                            "sl": new_sl,
                            "tp": pos.get('tp', 0.0)
                        }
                        res = mt5.order_send(req)
                        if res.retcode == mt5.TRADE_RETCODE_DONE:
                            self.log(f"🛡️ Trailing Stop: {symbol} Locked @ Breakeven")
            except Exception as e:
                self.log(f"Trail Error: {e}")

    def process_symbol(self, symbol):
        if symbol in self.active_tickets: return 

        if not self.check_trading_hours(symbol): return

        props = self.gateway.get_symbol_properties(symbol)
        if not props: return
        
        spread = (props['ask'] - props['bid']) / props['point']
        limit = 400 if "XAU" in symbol else 50
        if spread > limit: return 

        df = self.gateway.get_market_data(symbol)
        if df.empty: return

        try:
            candles = [Candle(**row) for row in df.to_dict('records') if hasattr(row['time'], 'year')]
            req = AnalysisRequest(symbol=symbol, candles=candles, daily_trend="NEUTRAL")
            analysis = analyze_market_structure(req)
            
            result_status = "SKIPPED"

            if analysis.signal != "NEUTRAL":
                 if analysis.confidence >= 0.85:
                     self.log(f"🔎 Signal Found: {symbol} {analysis.signal}")
                     result_status = "EXECUTED"
                     self.execute_signal(symbol, analysis, props)
                 else:
                     result_status = "LOW_CONFIDENCE"
            
            indicators = {"trend": analysis.trend, "reason": analysis.reason}
            DBManager.log_signal(symbol, analysis.signal, analysis.confidence, indicators, result_status)

        except: pass

    def calculate_lot_size(self, symbol, risk_amount, sl_dist):
        props = self.gateway.get_symbol_properties(symbol)
        if not props: return 0.01
        
        target_lot = 0.20 
        if "XAU" in symbol: target_lot = 0.15
        
        return max(props['min_lot'], min(props['max_lot'], target_lot))

    def execute_signal(self, symbol, analysis, props):
        is_buy = "BUY" in analysis.signal
        price = props['ask'] if is_buy else props['bid']
        
        spread_val = props['ask'] - props['bid']
        
        if "XAU" in symbol:
            min_dist = 5.00 
            raw_sl_dist = price * 0.005
        else:
            min_dist = 0.0025 * price 
            raw_sl_dist = price * 0.002

        sl_dist = max(raw_sl_dist, min_dist, spread_val * 4)
        
        stop_level = props.get('stops_level', 0) * props['point']
        sl_dist = max(sl_dist, stop_level + (props['point']*10))

        sl = price - sl_dist if is_buy else price + sl_dist
        tp = price + (sl_dist * 2) if is_buy else price - (sl_dist * 2)

        lot = self.calculate_lot_size(symbol, 0, 0)
        action = "BUY" if is_buy else "SELL"
        
        res = self.gateway.execute_trade(symbol, action, lot, sl, tp)
        
        if res and res["success"]:
            self.log(f"🚀 EXECUTE CONFIRMED: {symbol} | Lot: {lot}")
            self.async_alert(f"💎 **PRO EXEC**: {symbol} {action}\nLot: {lot}\nConfidence: {analysis.confidence*100:.0f}%")
            
            ticket = res.get('ticket', 0)
            DBManager.save_trade(ticket, symbol, action, lot, price, sl, tp, datetime.now())
            
        elif res:
            self.log(f"⚠️ BROKER REJECTED {symbol}: {res['message']}")

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
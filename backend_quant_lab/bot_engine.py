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
        
        # --- v50.1 GOLD SPECIALIST MODE ---
        self.vip_assets = [
            "EURUSD", "GBPUSD", "USDJPY", 
            "USDCAD", "USDCHF", "AUDUSD", "NZDUSD", 
            "XAUUSD"
        ]
        
        self.active_symbols = [] 
        self.MAX_OPEN_TRADES = 5 
        self.MAX_GOLD_TRADES = 1  # <--- NEW: Limit Gold Exposure
        self.TRAIL_TRIGGER = 200 
        
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

    # --- TELEGRAM COMMANDS ---
    def handle_telegram_command(self, command):
        cmd = command.split()[0].lower()
        self.log(f"📩 Received Command: {cmd}")
        
        if cmd == "/status":
            self._report_status()
        elif cmd == "/stop":
            self.stop_service()
            self.async_alert("🛑 **Bot Stopped by User Command**")
        elif cmd == "/start":
            if not self.is_running:
                self.start_service()
            else:
                self.async_alert("✅ Bot is already running.")
        elif cmd == "/balance":
            acc = self.gateway.get_account_info()
            if acc:
                self.async_alert(f"💰 **Balance:** ${acc['balance']:.2f}\n**Equity:** ${acc['equity']:.2f}")

    def _report_status(self):
        positions = self.gateway.get_open_positions()
        total_profit = sum(p['profit'] for p in positions)
        msg = f"📊 **System Status**\n\n"
        msg += f"**Active Trades:** {len(positions)}\n"
        msg += f"**Total PnL:** ${total_profit:.2f}\n"
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
        self.log(f"✅ TradeCore v50.1: Gold Specialist Active. Monitoring {len(self.active_symbols)} Assets.")
        
        # Start Listening to Telegram
        self.notifier.start_listening(self.handle_telegram_command)
        self.async_alert("🚀 **v50.1 Gold Specialist Online**\nMax Gold Trades: 1")
        return True

    def stop_service(self):
        self.is_running = False
        self.notifier.stop_listening()
        self.log("Stopped.")

    def run_cycle(self):
        if not self.is_running: return
        
        acc = self.gateway.get_account_info()
        if acc:
            DBManager.log_snapshot(acc['balance'], acc['equity'], acc['margin_level'], acc['free_margin'])
            
        current_positions = self.gateway.get_open_positions()
        self.apply_trailing_stop(current_positions)
        self.active_tickets = {p['symbol'] for p in current_positions}

        # --- NEW: Check Limits ---
        gold_trades = len([p for p in current_positions if "XAU" in p['symbol']])
        
        if len(current_positions) >= self.MAX_OPEN_TRADES:
            if datetime.now().second < 5: 
                self.log(f"⏸️ Capacity Full ({len(current_positions)}/{self.MAX_OPEN_TRADES}). Managing Trades.")
            return
        
        if datetime.now().second < 5:
            self.log(f"--- Scanning Markets (Forex > 75%, Gold > 80%) ---")
        
        for symbol in self.active_symbols:
            # Skip Gold if Quota Full
            if "XAU" in symbol and gold_trades >= self.MAX_GOLD_TRADES:
                continue
                
            self.process_symbol(symbol)

    def check_trading_hours(self, symbol):
        return True 

    def apply_trailing_stop(self, positions):
        for pos in positions:
            try:
                symbol = pos['symbol']
                ticket = pos['ticket']
                if 'open_price' not in pos: continue

                tick = mt5.symbol_info_tick(symbol)
                if not tick: continue
                
                price_current = tick.bid if pos['type'] == 'BUY' else tick.ask
                point = mt5.symbol_info(symbol).point
                
                # Dynamic Trailing Trigger: Gold needs more room
                trigger = 300 if "XAU" in symbol else 200
                
                if pos['type'] == 'BUY':
                    profit_points = (price_current - pos['open_price']) / point
                else:
                    profit_points = (pos['open_price'] - price_current) / point
                
                if profit_points > trigger:
                    # Gold locks tighter (10 pips), Forex looser (20 pips)
                    lock_dist = 10 if "XAU" in symbol else 20
                    new_sl = pos['open_price'] + (lock_dist * point) if pos['type'] == 'BUY' else pos['open_price'] - (lock_dist * point)
                    
                    current_sl = pos.get('sl', 0.0)
                    should_modify = False
                    if current_sl == 0: should_modify = True
                    elif pos['type'] == 'BUY' and new_sl > current_sl: should_modify = True
                    elif pos['type'] == 'SELL' and new_sl < current_sl: should_modify = True
                    
                    if should_modify:
                        req = {
                            "action": mt5.TRADE_ACTION_SLTP, "position": ticket,
                            "sl": new_sl, "tp": pos.get('tp', 0.0)
                        }
                        mt5.order_send(req)
                        self.log(f"🛡️ Trailing Stop: {symbol} Locked in Profit")
            except: pass

    def process_symbol(self, symbol):
        if symbol in self.active_tickets: return 

        props = self.gateway.get_symbol_properties(symbol)
        if not props: return
        
        spread = (props['ask'] - props['bid']) / props['point']
        limit = 600 if "XAU" in symbol else 60
        if spread > limit: return 

        df = self.gateway.get_market_data(symbol)
        if df.empty: return

        try:
            candles = [Candle(**row) for row in df.to_dict('records') if hasattr(row['time'], 'year')]
            req = AnalysisRequest(symbol=symbol, candles=candles, daily_trend="NEUTRAL")
            analysis = analyze_market_structure(req)
            
            result_status = "SKIPPED"
            
            # --- NEW: VIP CONFIDENCE ---
            required_conf = 0.80 if "XAU" in symbol else 0.75

            if analysis.signal != "NEUTRAL":
                 if analysis.confidence >= required_conf:
                     self.log(f"🔎 Signal Found: {symbol} {analysis.signal}")
                     result_status = "EXECUTED"
                     self.execute_signal(symbol, analysis, props)
                 else:
                     result_status = "LOW_CONFIDENCE"
            
            indicators = {"trend": analysis.trend, "reason": analysis.reason}
            DBManager.log_signal(symbol, analysis.signal, analysis.confidence, indicators, result_status)

        except: pass

    def calculate_lot_size(self, symbol):
        if "XAU" in symbol: return 0.20
        return 0.30

    def execute_signal(self, symbol, analysis, props):
        is_buy = "BUY" in analysis.signal
        price = props['ask'] if is_buy else props['bid']
        spread_val = props['ask'] - props['bid']
        
        if "XAU" in symbol:
            min_dist = 4.00 
            raw_sl_dist = price * 0.004
        else:
            min_dist = 0.0020 * price 
            raw_sl_dist = price * 0.002

        sl_dist = max(raw_sl_dist, min_dist, spread_val * 4)
        stop_level = props.get('stops_level', 0) * props['point']
        sl_dist = max(sl_dist, stop_level + (props['point']*10))

        sl = price - sl_dist if is_buy else price + sl_dist
        tp = price + (sl_dist * 2) if is_buy else price - (sl_dist * 2)

        lot = self.calculate_lot_size(symbol)
        action = "BUY" if is_buy else "SELL"
        
        res = self.gateway.execute_trade(symbol, action, lot, sl, tp)
        
        if res and res["success"]:
            self.log(f"🚀 EXECUTE CONFIRMED: {symbol} | Lot: {lot}")
            self.async_alert(f"🚀 **v50.1 EXEC**: {symbol} {action}\nLot: {lot}\nConf: {analysis.confidence*100:.0f}%")
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
import MetaTrader5 as mt5
import pandas as pd
from datetime import datetime, timedelta
import time
import re

class MT5Gateway:
    def __init__(self):
        self.connected = False
        self.symbol_map = {} 

    def start(self):
        if mt5.initialize():
            self.connected = True
            self._build_symbol_cache()
            return True
        try:
            if mt5.initialize(path=r"C:\Program Files\MetaTrader 5\terminal64.exe"):
                self.connected = True
                self._build_symbol_cache()
                return True
        except: pass
        self.connected = False
        print("❌ CRITICAL: MT5 Initialization Failed")
        return False

    def _build_symbol_cache(self):
        if self.symbol_map: return 
        symbols = mt5.symbols_get()
        if not symbols: return
        print(f"✅ Indexing {len(symbols)} Broker Assets...")
        for s in symbols:
            self.symbol_map[s.name] = s.name
            clean = s.name.split('.')[0].split('_')[0]
            if clean not in self.symbol_map: self.symbol_map[clean] = s.name
            simple = re.sub(r'[^a-zA-Z0-9]', '', s.name)
            if simple not in self.symbol_map: self.symbol_map[simple] = s.name

    def find_symbol(self, target):
        if not self.symbol_map: self._build_symbol_cache()
        if target in self.symbol_map: return self.symbol_map[target]
        for k, v in self.symbol_map.items():
            if k in target or target in k: return v
        return None

    def get_market_data(self, symbol, timeframe=mt5.TIMEFRAME_H1, n_candles=100):
        if not self.connected: self.start()
        real_symbol = self.find_symbol(symbol)
        if not real_symbol: return pd.DataFrame() 
        if not mt5.symbol_select(real_symbol, True): return pd.DataFrame()

        for _ in range(3):
            rates = mt5.copy_rates_from_pos(real_symbol, timeframe, 0, n_candles)
            if rates is not None and len(rates) > 0:
                df = pd.DataFrame(rates)
                df['time'] = pd.to_datetime(df['time'], unit='s')
                df.rename(columns={'tick_volume': 'volume'}, inplace=True)
                return df
            time.sleep(0.2)
        return pd.DataFrame()

    def get_symbol_properties(self, symbol):
        if not self.connected: self.start()
        real_symbol = self.find_symbol(symbol)
        if not real_symbol: return None
        i = mt5.symbol_info(real_symbol)
        if not i: return None
        stops_level = getattr(i, 'stops_level', 0)
        return {
            "name": i.name, "point": i.point, "trade_contract_size": i.trade_contract_size,
            "min_lot": i.volume_min, "max_lot": i.volume_max, "volume_step": i.volume_step,
            "ask": i.ask, "bid": i.bid, "filling_mode": i.filling_mode, "stops_level": stops_level
        }

    def execute_trade(self, symbol, action, lot, sl, tp):
        if not self.connected: self.start()
        real_symbol = self.find_symbol(symbol)
        if not real_symbol: return {"success": False, "message": "Symbol Not Found"}
        
        i = mt5.symbol_info(real_symbol)
        if i is None: return {"success": False, "message": "Symbol Info Failed"}

        fill = mt5.ORDER_FILLING_FOK if (i.filling_mode & 1) else mt5.ORDER_FILLING_IOC
        type_op = mt5.ORDER_TYPE_BUY if action == "BUY" else mt5.ORDER_TYPE_SELL
        price = i.ask if action == "BUY" else i.bid

        req = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": real_symbol,
            "volume": float(lot), "type": type_op, "price": price,
            "sl": float(sl), "tp": float(tp), "magic": 27000,
            "comment": "TradeCore v49", "type_time": mt5.ORDER_TIME_GTC, "type_filling": fill
        }
        res = mt5.order_send(req)
        
        if res is None: return {"success": False, "message": "Order Send Failed"}
             
        if res.retcode == mt5.TRADE_RETCODE_DONE:
            return {"success": True, "message": f"Opened {real_symbol}", "ticket": res.order}
        elif res.retcode == 10018: return {"success": False, "message": "Market Closed"}
        elif res.retcode == 10013: return {"success": False, "message": "Invalid Request"}
        else: return {"success": False, "message": f"MT5 Error: {res.comment} ({res.retcode})"}

    def close_position(self, ticket, symbol, volume, type_op):
        if not self.connected: self.start()
        real_symbol = self.find_symbol(symbol) or symbol
        is_buy = (type_op == "BUY" or type_op == 0)
        close_type = mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY
        tick = mt5.symbol_info_tick(real_symbol)
        if not tick: return False
        price = tick.bid if is_buy else tick.ask
        
        for mode in [mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_IOC]:
            req = {
                "action": mt5.TRADE_ACTION_DEAL, "position": ticket, "symbol": real_symbol,
                "volume": float(volume), "type": close_type, "price": price, "magic": 27000, "type_filling": mode
            }
            res = mt5.order_send(req)
            if res and res.retcode == mt5.TRADE_RETCODE_DONE: return True
        return False

    def get_account_info(self):
        if not self.connected: self.start()
        i = mt5.account_info()
        return {"balance": i.balance, "equity": i.equity, "profit": i.profit, "margin_level": i.margin_level, "free_margin": i.margin_free} if i else None

    def get_open_positions(self):
        if not self.connected: self.start()
        pos = mt5.positions_get() or []
        # FIX: Added 'open_price', 'sl', 'tp' to return dictionary
        return [{
            "ticket": p.ticket, 
            "symbol": p.symbol, 
            "profit": p.profit, 
            "volume": p.volume, 
            "type": "BUY" if p.type==0 else "SELL",
            "open_price": p.price_open,
            "sl": p.sl,
            "tp": p.tp
        } for p in pos]

    def get_historical_deals(self, days=30):
        if not self.connected: self.start()
        deals = mt5.history_deals_get(datetime.now()-timedelta(days=days), datetime.now()+timedelta(days=1)) or []
        return [{"symbol": d.symbol, "type": "BUY" if d.type==0 else "SELL", "volume": d.volume, "profit": d.profit, "time": datetime.fromtimestamp(d.time).strftime('%Y-%m-%d %H:%M')} for d in deals if d.entry==1]
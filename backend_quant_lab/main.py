# File: backend_quant_lab/main.py

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.background import BackgroundScheduler
from contextlib import asynccontextmanager
from models import SimulationRequest, SimulationResponse, AnalysisRequest, AnalysisResponse
from engine import run_monte_carlo
from analyst import analyze_market_structure, analyze_account_health
from bot_engine import TradingBot
import io
import csv
import pandas as pd
import os
from datetime import datetime
from fastapi.responses import StreamingResponse, PlainTextResponse

# Initialize the Bot Engine
bot = TradingBot()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Launch Bot & Scheduler
    bot.start_service()
    scheduler.start()
    yield
    # Shutdown: Clean up
    bot.notifier.send("⚠️ **System Shutdown**")
    bot.stop_service()
    scheduler.shutdown()

app = FastAPI(title="TradeCore v43", lifespan=lifespan)

# Scheduler for the 60-second Trading Loop
scheduler = BackgroundScheduler()
scheduler.add_job(bot.run_cycle, 'interval', seconds=60, id='trade_loop')

# CORS for Mobile/Web Access
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- TRADING ENDPOINTS ---

@app.get("/quant/scan_all")
async def scan_market():
    """Returns opportunities. Defaults to VIP list if engine is warming up."""
    opportunities = []
    
    # FIX: "Hot Start" - Use VIP assets if the active list is empty
    source = bot.active_symbols if bot.active_symbols else bot.vip_assets
    
    # Limit to 50 to prevent timeout
    for symbol in source[:50]: 
        df = bot.gateway.get_market_data(symbol)
        if df.empty: continue
        try:
            from models import Candle
            candles = [Candle(**row) for row in df.to_dict('records')]
            req = AnalysisRequest(symbol=symbol, candles=candles)
            res = analyze_market_structure(req)
            
            # Return valid signals
            if "BUY" in res.signal or "SELL" in res.signal:
                opportunities.append(res.dict())
        except: continue
    
    return {"opportunities": opportunities}

@app.get("/quant/market_data/{symbol}")
async def get_market_detail(symbol: str):
    """Fetches chart data for the frontend"""
    df = bot.gateway.get_market_data(symbol, n_candles=200)
    if df.empty or len(df) < 50: raise HTTPException(404, "Insufficient Data")
    
    high = df['high'].max()
    low = df['low'].min()
    diff = high - low
    fib_levels = { "0.0%": low, "38.2%": low + 0.382*diff, "50.0%": low + 0.5*diff, "61.8%": low + 0.618*diff, "100.0%": high }
    
    tenkan = (df['high'].rolling(9).max().iloc[-1] + df['low'].rolling(9).min().iloc[-1]) / 2
    kijun = (df['high'].rolling(26).max().iloc[-1] + df['low'].rolling(26).min().iloc[-1]) / 2

    chart_data = [{"date": int(r['time'].timestamp()*1000), "open": r['open'], "high": r['high'], "low": r['low'], "close": r['close'], "volume": float(r['volume'])} for _, r in df.tail(100).iterrows()]
        
    return {
        "symbol": symbol, "current_price": df['close'].iloc[-1], "fibonacci": fib_levels,
        "ichimoku": {"tenkan_sen": tenkan, "kijun_sen": kijun, "status": "Bullish" if tenkan > kijun else "Bearish"},
        "chart_data": chart_data
    }

@app.post("/quant/ai_setup")
async def ai_setup(data: dict):
    """Calculates risk-based lot size"""
    symbol = data.get("symbol")
    risk = float(data.get("risk", 50.0))
    props = bot.gateway.get_symbol_properties(symbol)
    if not props: raise HTTPException(404, "Symbol not found")
    
    price = props['ask'] 
    if "USD" in symbol and len(symbol)==6: sl_dist = price * 0.002 
    elif "BTC" in symbol: sl_dist = price * 0.005 
    else: sl_dist = max(price * 0.01, 0.50)
    
    min_stop = props.get('stops_level', 0) * props['point']
    if sl_dist < min_stop: sl_dist = min_stop * 1.5
    
    lot = bot.calculate_lot_size(symbol, risk, sl_dist)
    return {"symbol": symbol, "ai_lot": lot, "sl_distance": round(sl_dist, 5), "stop_loss_price": round(price - sl_dist, 5), "take_profit_price": round(price + (sl_dist * 2), 5), "risk_reward": "1:2"}

@app.post("/quant/execute_manual")
async def manual_trade(trade: dict):
    """Executes a trade manually via the App"""
    lot = float(trade.get('lot_size', 0.01))
    symbol = trade['symbol']
    signal = trade['signal']
    props = bot.gateway.get_symbol_properties(symbol)
    if not props: return {"status": "Error", "message": "Symbol not found"}
    
    price = props['ask'] if "BUY" in signal else props['bid']
    sl_dist = price * 0.002
    sl = price - sl_dist if "BUY" in signal else price + sl_dist
    tp = price + (sl_dist * 2) if "BUY" in signal else price - (sl_dist * 2)

    result = bot.gateway.execute_trade(symbol, "BUY" if "BUY" in signal else "SELL", lot, sl, tp)
    
    if result["success"]:
        bot.notifier.send(f"📱 **Manual Exec**: {symbol} {signal}\n✅ {result['message']}")
        return {"status": "Executed", "message": result['message']}
    else:
        bot.notifier.send(f"⚠️ **Manual Fail**: {symbol}\n❌ {result['message']}")
        return {"status": "Error", "message": result['message']}

# --- AUDIT & SYSTEM ENDPOINTS ---

@app.get("/quant/audit")
async def audit():
    """Robust Audit that handles empty/zero states"""
    stats = {
        "net_profit": 0.0, "total_trades": 0, "win_rate": 0.0, 
        "profit_factor": 0.0, "equity_curve": [], "source": "Waiting for Data"
    }

    try:
        deals = bot.gateway.get_historical_deals(days=30)
        if deals:
            df = pd.DataFrame(deals)
            df['profit'] = pd.to_numeric(df['profit'], errors='coerce').fillna(0)
            df['equity'] = df['profit'].cumsum()
            
            total_trades = len(df)
            wins = df[df['profit'] > 0]
            losses = df[df['profit'] < 0]
            
            net_profit = df['profit'].sum()
            win_rate = (len(wins) / total_trades * 100) if total_trades > 0 else 0.0
            
            gross_profit = wins['profit'].sum()
            gross_loss = abs(losses['profit'].sum())
            
            if gross_loss == 0:
                profit_factor = 99.9 if gross_profit > 0 else 0.0
            else:
                profit_factor = gross_profit / gross_loss
            
            stats = {
                "net_profit": round(net_profit, 2),
                "total_trades": total_trades,
                "win_rate": round(win_rate, 1),
                "profit_factor": round(profit_factor, 2),
                "equity_curve": df['equity'].tolist(),
                "source": "Live API"
            }
    except Exception as e:
        print(f"Audit Error: {e}")

    return stats

@app.get("/system/logs")
async def get_system_logs():
    """Downloads logs for debugging"""
    log_content = "\n".join(bot.logs)
    status = bot.get_status()
    acc = status.get('account') or {'balance': 0, 'equity': 0}
    report = f"""--- TRADECORE SYSTEM REPORT ---\nGenerated: {datetime.now()}\nStatus: {'ONLINE' if status['is_running'] else 'OFFLINE'}\n\n--- ACCOUNT ---\nBalance: {acc['balance']}\nEquity: {acc['equity']}\n\n--- LIVE LOGS ---\n{log_content}"""
    return PlainTextResponse(report)

@app.get("/quant/export_report")
async def export_report():
    """Downloads trade history as CSV"""
    deals = bot.gateway.get_historical_deals(days=30)
    stream = io.StringIO(); writer = csv.writer(stream)
    writer.writerow(["Time", "Symbol", "Type", "Volume", "Profit"])
    for d in deals: writer.writerow([d['time'], d['symbol'], d['type'], d['volume'], d['profit']])
    return StreamingResponse(iter([stream.getvalue()]), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=TradeCore_Report.csv"})

@app.post("/bot/start")
async def start_bot(): return {"status": "Started"} if bot.start_service() else HTTPException(500)
@app.post("/bot/stop")
async def stop_bot(): bot.stop_service(); return {"status": "Stopped"}
@app.get("/bot/status")
async def get_bot_status(): return bot.get_status()
@app.post("/simulate", response_model=SimulationResponse)
async def simulate(request: SimulationRequest): return run_monte_carlo(request)
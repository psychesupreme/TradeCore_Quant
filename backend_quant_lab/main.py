import traceback
import io
import csv
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, PlainTextResponse
from apscheduler.schedulers.background import BackgroundScheduler

from bot_engine import TradingBot
from sync_db import sync_database

# Initialize the Global Singleton Bot Engine
bot = TradingBot()
scheduler = BackgroundScheduler()

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("\n" + "="*50)
    print("🚀 System Startup: Initializing TradeCore v53.0...")
    print("="*50)
    
    try:
        if bot.start_service():
            print("✅ Bot Service Started Successfully")
        else:
            print("❌ Bot Service Failed to Start")

        if not scheduler.get_jobs():
            scheduler.add_job(bot.run_cycle, 'interval', seconds=60, id='trade_loop')
            scheduler.add_job(sync_database, 'interval', minutes=5, id='db_cleaner')
            # [S9] Daily Telegram summary at 23:50 UTC — operator accountability report.
            # Fires once per day so autopilot can be monitored from a phone.
            scheduler.add_job(
                bot.send_daily_summary,
                'cron', hour=23, minute=50,
                id='daily_summary', timezone='UTC'
            )
            scheduler.start()
            print("✅ Scheduler Active: Trading Loop, DB Sync & Daily Summary Online.")
            
    except Exception as e:
        print(f"❌ CRITICAL STARTUP ERROR: {e}")
        traceback.print_exc()
        
    yield
    
    print("\n⚠️ System Shutdown...")
    bot.stop_service()
    scheduler.shutdown()

app = FastAPI(title="TradeCore v53.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/bot/status")
async def get_bot_status():
    try:
        return bot.get_status()
    except Exception as e:
        print("\n❌ API ERROR on /bot/status:")
        traceback.print_exc() 
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/bot/news")
async def get_news():
    try:
        bot.news_manager.fetch_calendar()
        events = bot.news_manager.events
        return [e for e in events if e['impact'] in ['High', 'Medium']]
    except Exception as e:
        print(f"\n❌ API ERROR on /bot/news: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch news data")

@app.get("/bot/performance")
async def get_performance():
    try:
        import pandas as pd
        
        STARTING_DEFICIT = 0.0
        curve_data = [{"date": "Start", "profit": STARTING_DEFICIT}]
        
        total_realized = 0.0
        monthly_realized = 0.0
        
        # --- NEW BOT AUDIT METRICS ---
        win_rate = 0.0
        profit_factor = 0.0
        total_trades = 0
        
        deals = bot.gateway.get_historical_deals(days=365)
        
        if deals:
            df = pd.DataFrame(deals)
            if 'profit' in df.columns:
                total_realized = float(df['profit'].sum())
                
                df['time'] = pd.to_datetime(df['time'])
                now = datetime.now()
                monthly_df = df[(df['time'].dt.month == now.month) & (df['time'].dt.year == now.year)]
                monthly_realized = float(monthly_df['profit'].sum())

                # --- CALCULATE WIN RATE & PROFIT FACTOR ---
                wins = df[df['profit'] > 0]
                losses = df[df['profit'] < 0]
                
                gross_profit = wins['profit'].sum() if not wins.empty else 0.0
                gross_loss = abs(losses['profit'].sum()) if not losses.empty else 0.0
                
                total_trades = len(df)
                if total_trades > 0:
                    win_rate = round((len(wins) / total_trades) * 100, 1)
                
                if gross_loss > 0:
                    profit_factor = round(gross_profit / gross_loss, 2)
                elif gross_profit > 0:
                    profit_factor = 99.9 # Mathematically perfect if no losses

                df['cumulative_profit'] = df['profit'].cumsum() + STARTING_DEFICIT
                df['date'] = df['time'].dt.strftime('%m-%d %H:%M')
                
                trade_points = df[['date', 'cumulative_profit']].rename(
                    columns={'cumulative_profit': 'profit'}
                ).to_dict(orient='records')
                curve_data.extend(trade_points)

        if len(curve_data) < 2:
            curve_data.append({
                "date": datetime.now().strftime('%m-%d %H:%M'),
                "profit": STARTING_DEFICIT
            })

        return {
            "total_realized": total_realized,
            "monthly_realized": monthly_realized,
            "win_rate": win_rate,
            "profit_factor": profit_factor,
            "total_trades": total_trades,
            "curve": curve_data
        }
    except Exception as e:
        print(f"\n❌ API ERROR on /bot/performance:")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Failed to fetch MT5 performance data")

@app.get("/quant/export_report")
async def export_report():
    try:
        deals = bot.gateway.get_historical_deals(days=365)
        output = io.StringIO()
        writer = csv.writer(output)
        
        writer.writerow(["System", "TradeCore v53.0 Quant Auditor"])
        writer.writerow(["Generated", datetime.now().strftime("%Y-%m-%d %H:%M")])
        writer.writerow([])
        writer.writerow(["Close Time", "Symbol", "Action", "Volume", "Profit ($)"])
        
        for d in deals:
            writer.writerow([d['time'], d['symbol'], d['type'], d['volume'], d['profit']])
            
        response = StreamingResponse(iter([output.getvalue()]), media_type="text/csv")
        response.headers["Content-Disposition"] = f"attachment; filename=TradeCore_Audit_{datetime.now().strftime('%Y%m%d')}.csv"
        return response
    except Exception as e:
        print(f"❌ AUDIT ERROR: {e}")
        raise HTTPException(status_code=500, detail="Failed to generate CSV audit")

@app.get("/system/logs")
async def get_system_logs():
    log_content = "\n".join(bot.logs)
    status = bot.get_status()
    acc = status.get('account') or {'balance': 0, 'equity': 0}
    
    report = f"""--- TRADECORE SYSTEM REPORT ---
Generated: {datetime.now()}
Status: {'ONLINE' if status['is_running'] else 'OFFLINE'}

--- ACCOUNT ---
Balance: {acc['balance']}
Equity: {acc['equity']}

--- LIVE LOGS ---
{log_content}"""
    return PlainTextResponse(report)
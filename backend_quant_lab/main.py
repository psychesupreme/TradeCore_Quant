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
    """Handles system startup and shutdown events"""
    print("\n" + "="*50)
    print("🚀 System Startup: Initializing TradeCore Recovery Engine...")
    print("="*50)
    
    try:
        # 1. Start the MT5 Connection & Bot Service
        if bot.start_service():
            print("✅ Bot Service Started Successfully")
        else:
            print("❌ Bot Service Failed to Start")

        # 2. Configure the Background Scheduler
        if not scheduler.get_jobs():
            # Core Trading Loop: Every 60 seconds
            scheduler.add_job(bot.run_cycle, 'interval', seconds=60, id='trade_loop')
            
            # Automated Database Cleanup: Every 5 minutes
            # (Ensures local DB matches MT5 closed trades)
            scheduler.add_job(sync_database, 'interval', minutes=5, id='db_cleaner')
            
            scheduler.start()
            print("✅ Scheduler Active: Trading Loop & DB Sync Online.")
            
    except Exception as e:
        print(f"❌ CRITICAL STARTUP ERROR: {e}")
        traceback.print_exc()
        
    yield
    
    # --- SHUTDOWN ---
    print("\n⚠️ System Shutdown...")
    bot.stop_service()
    scheduler.shutdown()

app = FastAPI(title="TradeCore v51.0 Recovery Edition", lifespan=lifespan)

# Allow Flutter Web/Mobile to communicate with this API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/bot/status")
async def get_bot_status():
    """Provides live MT5 account vitals to the Flutter Terminal"""
    try:
        return bot.get_status()
    except Exception as e:
        print("\n❌ API ERROR on /bot/status:")
        traceback.print_exc() 
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/bot/news")
async def get_news():
    """Returns high-impact economic events for the News Guard tab"""
    try:
        bot.news_manager.fetch_calendar()
        events = bot.news_manager.events
        # Only return High/Medium impact to keep the UI focused on risk
        return [e for e in events if e['impact'] in ['High', 'Medium']]
    except Exception as e:
        print(f"\n❌ API ERROR on /bot/news: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch news data")

@app.get("/bot/performance")
async def get_performance():
    """Calculates cumulative recovery trajectory against the $19k deficit"""
    try:
        import pandas as pd
        
        # The historical deficit we are recovering from
        STARTING_DEFICIT = -19000.0
        
        # Initialize with Point Zero so the chart can draw immediately
        curve_data = [{"date": "Start", "profit": STARTING_DEFICIT}]
        
        total_realized = 0.0
        monthly_realized = 0.0
        
        # Fetch full account history from the broker
        deals = bot.gateway.get_historical_deals(days=365)
        
        if deals:
            df = pd.DataFrame(deals)
            if 'profit' in df.columns:
                total_realized = float(df['profit'].sum())
                
                # Monthly stats for the recovery cards
                df['time'] = pd.to_datetime(df['time'])
                now = datetime.now()
                monthly_df = df[(df['time'].dt.month == now.month) & (df['time'].dt.year == now.year)]
                monthly_realized = float(monthly_df['profit'].sum())

                # Build the recovery curve starting from the deficit
                df['cumulative_profit'] = df['profit'].cumsum() + STARTING_DEFICIT
                df['date'] = df['time'].dt.strftime('%m-%d %H:%M')
                
                trade_points = df[['date', 'cumulative_profit']].rename(
                    columns={'cumulative_profit': 'profit'}
                ).to_dict(orient='records')
                curve_data.extend(trade_points)

        # Safety: Ensure at least two points exist for fl_chart to render
        if len(curve_data) < 2:
            curve_data.append({
                "date": datetime.now().strftime('%m-%d %H:%M'),
                "profit": STARTING_DEFICIT
            })

        return {
            "total_realized": total_realized,
            "monthly_realized": monthly_realized,
            "curve": curve_data
        }
    except Exception as e:
        print(f"\n❌ API ERROR on /bot/performance:")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Failed to fetch MT5 performance data")

@app.get("/quant/export_report")
async def export_report():
    """Generates a professional CSV audit of the 365-day account history"""
    try:
        deals = bot.gateway.get_historical_deals(days=365)
        output = io.StringIO()
        writer = csv.writer(output)
        
        # Professional Metadata Headers
        writer.writerow(["System", "TradeCore v51.0 Quant Auditor"])
        writer.writerow(["Generated", datetime.now().strftime("%Y-%m-%d %H:%M")])
        writer.writerow([])
        writer.writerow(["Close Time", "Symbol", "Action", "Volume", "Profit ($)"])
        
        for d in deals:
            writer.writerow([d['time'], d['symbol'], d['type'], d['volume'], d['profit']])
            
        # Return as a downloadable file stream
        response = StreamingResponse(iter([output.getvalue()]), media_type="text/csv")
        response.headers["Content-Disposition"] = f"attachment; filename=TradeCore_Audit_{datetime.now().strftime('%Y%m%d')}.csv"
        return response
    except Exception as e:
        print(f"❌ AUDIT ERROR: {e}")
        raise HTTPException(status_code=500, detail="Failed to generate CSV audit")

@app.get("/system/logs")
async def get_system_logs():
    """Returns a plain text report of the latest bot logs for debugging"""
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
# ============================================================
# TradeCore v51.0 — main.py
# SPRINT 1 FIXES APPLIED:
#   [BUG-01] APScheduler max_instances=1 + coalesce=True
#   [BUG-07] Performance endpoint 60s server-side cache
#   [BUG-11] API Key authentication on all endpoints
#   [BUG-11] CORS locked to localhost origins only
# ============================================================

import traceback
import io
import csv
import time as _time
import os
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Security, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, PlainTextResponse
from fastapi.security.api_key import APIKeyHeader
from apscheduler.schedulers.background import BackgroundScheduler

from bot_engine import TradingBot
from sync_db import sync_database

# ============================================================
# [BUG-11] API KEY AUTHENTICATION
# Set environment variable TRADECORE_API_KEY before starting.
# e.g. Windows:  set TRADECORE_API_KEY=your_secret_key_here
# e.g. Linux:    export TRADECORE_API_KEY=your_secret_key_here
#
# PAPER ACCOUNT MODE: If no key is set, defaults to "dev-paper"
# so the system starts without configuration. Change this for
# any live account deployment.
# ============================================================
_API_KEY = os.environ.get("TRADECORE_API_KEY", "dev-paper")
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

async def require_api_key(key: str = Security(_api_key_header)):
    if key != _API_KEY:
        raise HTTPException(
            status_code=403,
            detail="Forbidden: Invalid or missing X-API-Key header."
        )
    return key

# ============================================================
# [BUG-07] PERFORMANCE CACHE
# Prevents /bot/performance from hammering MT5 with a full
# history query on every Flutter 3-second poll.
# ============================================================
_perf_cache = {"data": None, "ts": 0.0}
_PERF_CACHE_TTL = 60  # seconds

# ============================================================
# GLOBAL SINGLETONS
# ============================================================
bot = TradingBot()
scheduler = BackgroundScheduler()

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("\n" + "="*55)
    print("🚀 TradeCore v51.0 — System Startup")
    print("="*55)

    # Validate critical environment on startup
    if _API_KEY == "dev-paper":
        print("⚠️  WARNING: Running with default dev-paper API key.")
        print("   Set TRADECORE_API_KEY env var for production.")
    else:
        print("✅ API Key: Loaded from environment.")

    try:
        if bot.start_service():
            print("✅ Bot Service Started Successfully")
        else:
            print("❌ Bot Service Failed to Start — Check MT5 connection.")

        if not scheduler.get_jobs():
            # [BUG-01] max_instances=1 prevents overlapping cycle runs.
            # coalesce=True means if a cycle is missed during a long run,
            # it fires once on recovery rather than stacking up.
            scheduler.add_job(
                bot.run_cycle,
                'interval',
                seconds=60,
                id='trade_loop',
                max_instances=1,
                coalesce=True
            )
            # DB sync runs every 5 minutes to close ghost trades.
            # max_instances=1 here too — sync should never stack.
            scheduler.add_job(
                sync_database,
                'interval',
                minutes=5,
                id='db_cleaner',
                max_instances=1,
                coalesce=True
            )
            scheduler.start()
            print("✅ Scheduler: Trade loop (60s) and DB sync (5min) active.")

    except Exception as e:
        print(f"❌ CRITICAL STARTUP ERROR: {e}")
        traceback.print_exc()

    yield

    print("\n⚠️  System Shutdown — Stopping all services...")
    bot.stop_service()
    scheduler.shutdown()
    print("✅ Shutdown complete.")


app = FastAPI(
    title="TradeCore v51.0",
    description="Autonomous SMC Trading System — Paper Account",
    version="51.0",
    lifespan=lifespan
)

# [BUG-11] Restrict CORS to known origins.
# For paper account development, localhost origins are sufficient.
# Add your Flutter web origin here if deploying as a web app.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://localhost:3000",   # Flutter web dev server
        "http://10.0.2.2:8000",   # Android emulator → host
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ============================================================
# ENDPOINTS
# All endpoints require the X-API-Key header.
# Flutter must send: headers: {'X-API-Key': YOUR_KEY}
# ============================================================

@app.get("/bot/status", dependencies=[Depends(require_api_key)])
async def get_bot_status():
    """Live account status, positions, regime, and VaR. Fast endpoint — no MT5 history call."""
    try:
        return bot.get_status()
    except Exception as e:
        print("\n❌ API ERROR on /bot/status:")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/bot/news", dependencies=[Depends(require_api_key)])
async def get_news():
    """Upcoming high/medium impact news events with CEO-level insights."""
    try:
        bot.news_manager.fetch_calendar()
        events = bot.news_manager.events
        return [e for e in events if e['impact'] in ['High', 'Medium']]
    except Exception as e:
        print(f"\n❌ API ERROR on /bot/news: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch news data")


@app.get("/bot/performance", dependencies=[Depends(require_api_key)])
async def get_performance():
    """
    Historical performance metrics: win rate, PF, equity curve.

    [BUG-07 FIX] Response is cached for 60 seconds server-side.
    Flutter should poll this at 30s intervals, not 3s.
    The /bot/status endpoint is the fast live data source.
    """
    # Serve from cache if still fresh
    if _perf_cache["data"] is not None and (_time.time() - _perf_cache["ts"]) < _PERF_CACHE_TTL:
        return _perf_cache["data"]

    try:
        import pandas as pd

        STARTING_DEFICIT = 0.0
        curve_data = [{"date": "Start", "profit": STARTING_DEFICIT}]

        total_realized   = 0.0
        monthly_realized = 0.0
        win_rate         = 0.0
        profit_factor    = 0.0
        total_trades     = 0

        deals = bot.gateway.get_historical_deals(days=365)

        if deals:
            df = pd.DataFrame(deals)
            if 'profit' in df.columns:
                total_realized = float(df['profit'].sum())

                df['time'] = pd.to_datetime(df['time'])
                now = datetime.now()
                monthly_df = df[
                    (df['time'].dt.month == now.month) &
                    (df['time'].dt.year == now.year)
                ]
                monthly_realized = float(monthly_df['profit'].sum())

                wins   = df[df['profit'] > 0]
                losses = df[df['profit'] < 0]

                gross_profit = wins['profit'].sum()   if not wins.empty   else 0.0
                gross_loss   = abs(losses['profit'].sum()) if not losses.empty else 0.0

                total_trades = len(df)
                if total_trades > 0:
                    win_rate = round((len(wins) / total_trades) * 100, 1)

                if gross_loss > 0:
                    profit_factor = round(gross_profit / gross_loss, 2)
                elif gross_profit > 0:
                    profit_factor = 99.9

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

        result = {
            "total_realized":   total_realized,
            "monthly_realized": monthly_realized,
            "win_rate":         win_rate,
            "profit_factor":    profit_factor,
            "total_trades":     total_trades,
            "curve":            curve_data,
            "cached_at":        datetime.now().strftime('%H:%M:%S')
        }

        # Store in cache
        _perf_cache["data"] = result
        _perf_cache["ts"]   = _time.time()

        return result

    except Exception as e:
        print(f"\n❌ API ERROR on /bot/performance:")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Failed to fetch MT5 performance data")


@app.get("/quant/export_report", dependencies=[Depends(require_api_key)])
async def export_report():
    """Download full trade history as a CSV audit file."""
    try:
        deals = bot.gateway.get_historical_deals(days=365)
        output = io.StringIO()
        writer = csv.writer(output)

        writer.writerow(["System",    "TradeCore v51.0 Quant Auditor"])
        writer.writerow(["Generated", datetime.now().strftime("%Y-%m-%d %H:%M")])
        writer.writerow(["Account",   "Paper Trading"])
        writer.writerow([])
        writer.writerow(["Close Time", "Symbol", "Action", "Volume", "Profit ($)"])

        for d in deals:
            writer.writerow([d['time'], d['symbol'], d['type'], d['volume'], d['profit']])

        filename = f"TradeCore_Audit_{datetime.now().strftime('%Y%m%d')}.csv"
        response = StreamingResponse(iter([output.getvalue()]), media_type="text/csv")
        response.headers["Content-Disposition"] = f"attachment; filename={filename}"
        return response

    except Exception as e:
        print(f"❌ AUDIT ERROR: {e}")
        raise HTTPException(status_code=500, detail="Failed to generate CSV audit")


@app.get("/quant/simulate", dependencies=[Depends(require_api_key)])
async def simulate_placeholder():
    """
    Monte Carlo simulation endpoint.
    Sprint 4 will wire engine.py's run_monte_carlo() here.
    Currently returns a placeholder so the endpoint is discoverable.
    """
    return {
        "status": "pending",
        "message": "Monte Carlo endpoint active. POST body with SimulationRequest to use. Wired in Sprint 4."
    }


@app.get("/system/logs", dependencies=[Depends(require_api_key)])
async def get_system_logs():
    """Plain-text system report with live balance and recent log lines."""
    log_content = "\n".join(bot.logs)
    status = bot.get_status()
    acc = status.get('account') or {'balance': 0, 'equity': 0}

    report = (
        f"--- TRADECORE v51.0 SYSTEM REPORT ---\n"
        f"Generated : {datetime.now()}\n"
        f"Status    : {'ONLINE' if status['is_running'] else 'OFFLINE'}\n"
        f"Regime    : {status.get('market_regime', 'UNKNOWN')}\n"
        f"Daily VaR : ${status.get('daily_var', 0):.2f}\n"
        f"\n--- ACCOUNT ---\n"
        f"Balance    : ${acc['balance']:,.2f}\n"
        f"Equity     : ${acc['equity']:,.2f}\n"
        f"\n--- LIVE LOGS (last 100 events) ---\n"
        f"{log_content}"
    )
    return PlainTextResponse(report)


@app.get("/health")
async def health():
    """Public health check — no auth required. Used by monitoring tools."""
    return {"status": "ok", "version": "51.0", "mode": "paper"}

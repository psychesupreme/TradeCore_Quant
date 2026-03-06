# ============================================================
# TradeCore v51.0 — main.py
# SPRINT 1 FIXES:  BUG-01, BUG-07, BUG-11
# HOTFIX APPLIED:
#   [HF-A] CORS OPTIONS 400 — replaced per-route Depends() with
#          a single HTTP middleware that skips OPTIONS preflights.
#          CORS allow_origins changed to ["*"] for paper/dev.
#          Per-route Depends(require_api_key) REMOVED everywhere.
# ============================================================

import traceback
import io
import csv
import time as _time
import os
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, PlainTextResponse, JSONResponse
from apscheduler.schedulers.background import BackgroundScheduler

from bot_engine import TradingBot
from sync_db import sync_database
from engine import run_monte_carlo
from models import SimulationRequest

# ============================================================
# [BUG-11] API KEY
# ============================================================
_API_KEY = os.environ.get("TRADECORE_API_KEY", "dev-paper")

# ============================================================
# [BUG-07] PERFORMANCE CACHE
# ============================================================
_perf_cache = {"data": None, "ts": 0.0}
_PERF_CACHE_TTL = 60  # seconds

# ============================================================
# GLOBAL SINGLETONS
# ============================================================
bot       = TradingBot()
scheduler = BackgroundScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("\n" + "=" * 55)
    print("🚀 TradeCore v51.0 — System Startup")
    print("=" * 55)

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
            scheduler.add_job(
                bot.run_cycle, "interval", seconds=60,
                id="trade_loop", max_instances=1, coalesce=True
            )
            scheduler.add_job(
                sync_database, "interval", minutes=5,
                id="db_cleaner", max_instances=1, coalesce=True
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
    lifespan=lifespan,
)

# ============================================================
# [HF-A] CORS — wildcard origins for paper/dev environment.
# The API key IS the security layer. CORS origin restriction
# is irrelevant for a localhost server; it was causing every
# Flutter OPTIONS preflight to receive 400 Bad Request.
# For a live deployment tighten allow_origins to specific hosts.
# ============================================================
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,   # must be False when allow_origins=["*"]
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


# ============================================================
# [HF-A] SINGLE API KEY MIDDLEWARE
# Replaces per-route Depends(require_api_key).
# OPTIONS preflights bypass auth — the browser never sends
# custom headers on the preflight request itself.
# ============================================================
@app.middleware("http")
async def enforce_api_key(request: Request, call_next):
    # Always pass OPTIONS through — CORS middleware handles it
    if request.method == "OPTIONS":
        return await call_next(request)

    # Health check is public
    if request.url.path == "/health":
        return await call_next(request)

    key = request.headers.get("X-API-Key", "")
    if key != _API_KEY:
        return JSONResponse(
            status_code=403,
            content={"detail": "Forbidden: Invalid or missing X-API-Key header."},
        )
    return await call_next(request)


# ============================================================
# ENDPOINTS  (Depends(require_api_key) removed — middleware handles auth)
# ============================================================

@app.get("/bot/status")
async def get_bot_status():
    """Live account status, positions, regime, and VaR."""
    try:
        return bot.get_status()
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/bot/news")
async def get_news():
    """
    Upcoming high/medium impact news events.
    Uses the cached calendar — does NOT force a re-fetch on every call.
    Re-fetch happens on the hourly scheduler cycle or bot startup.
    """
    try:
        # Use stale-while-revalidate: return what we have, let
        # the background scheduler keep it fresh every hour.
        events = bot.news_manager.events
        return [e for e in events if e["impact"] in ["High", "Medium"]]
    except Exception as e:
        print(f"\n❌ API ERROR on /bot/news: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch news data")


@app.get("/bot/performance")
async def get_performance():
    """
    Historical performance metrics: win rate, PF, equity curve.
    [BUG-07 FIX] Cached for 60 seconds server-side.
    """
    if _perf_cache["data"] is not None and (_time.time() - _perf_cache["ts"]) < _PERF_CACHE_TTL:
        return _perf_cache["data"]

    try:
        import pandas as pd

        STARTING_DEFICIT = 0.0
        curve_data       = [{"date": "Start", "profit": STARTING_DEFICIT}]
        total_realized   = 0.0
        monthly_realized = 0.0
        win_rate         = 0.0
        profit_factor    = 0.0
        total_trades     = 0

        deals = bot.gateway.get_historical_deals(days=365)

        if deals:
            df = pd.DataFrame(deals)
            if "profit" in df.columns:
                total_realized = float(df["profit"].sum())

                df["time"]   = pd.to_datetime(df["time"])
                now          = datetime.now()
                monthly_df   = df[
                    (df["time"].dt.month == now.month) &
                    (df["time"].dt.year  == now.year)
                ]
                monthly_realized = float(monthly_df["profit"].sum())

                wins   = df[df["profit"] > 0]
                losses = df[df["profit"] < 0]

                gross_profit = wins["profit"].sum()          if not wins.empty   else 0.0
                gross_loss   = abs(losses["profit"].sum())   if not losses.empty else 0.0

                total_trades = len(df)
                if total_trades > 0:
                    win_rate = round((len(wins) / total_trades) * 100, 1)
                if gross_loss > 0:
                    profit_factor = round(gross_profit / gross_loss, 2)
                elif gross_profit > 0:
                    profit_factor = 99.9

                df["cumulative_profit"] = df["profit"].cumsum() + STARTING_DEFICIT
                df["date"] = df["time"].dt.strftime("%m-%d %H:%M")
                trade_points = df[["date", "cumulative_profit"]].rename(
                    columns={"cumulative_profit": "profit"}
                ).to_dict(orient="records")
                curve_data.extend(trade_points)

        if len(curve_data) < 2:
            curve_data.append({
                "date":   datetime.now().strftime("%m-%d %H:%M"),
                "profit": STARTING_DEFICIT,
            })

        result = {
            "total_realized":   total_realized,
            "monthly_realized": monthly_realized,
            "win_rate":         win_rate,
            "profit_factor":    profit_factor,
            "total_trades":     total_trades,
            "curve":            curve_data,
            "cached_at":        datetime.now().strftime("%H:%M:%S"),
        }

        _perf_cache["data"] = result
        _perf_cache["ts"]   = _time.time()
        return result

    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Failed to fetch MT5 performance data")


@app.get("/quant/export_report")
async def export_report():
    """Download full trade history as a CSV audit file."""
    try:
        deals    = bot.gateway.get_historical_deals(days=365)
        output   = io.StringIO()
        writer   = csv.writer(output)
        writer.writerow(["System",    "TradeCore v51.0 Quant Auditor"])
        writer.writerow(["Generated", datetime.now().strftime("%Y-%m-%d %H:%M")])
        writer.writerow(["Account",   "Paper Trading"])
        writer.writerow([])
        writer.writerow(["Close Time", "Symbol", "Action", "Volume", "Profit ($)"])
        for d in deals:
            writer.writerow([d["time"], d["symbol"], d["type"], d["volume"], d["profit"]])

        filename = f"TradeCore_Audit_{datetime.now().strftime('%Y%m%d')}.csv"
        response = StreamingResponse(iter([output.getvalue()]), media_type="text/csv")
        response.headers["Content-Disposition"] = f"attachment; filename={filename}"
        return response

    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to generate CSV audit")


@app.post("/quant/simulate")
async def simulate(request: SimulationRequest):
    """
    Monte Carlo simulation — 1,000 equity paths.

    POST body (JSON):
      {
        "initial_balance": 10000,
        "risk_per_trade":  0.02,
        "win_rate":        0.55,
        "reward_ratio":    1.5,
        "total_trades":    100
      }

    Returns median final balance, max drawdown, probability of ruin,
    and a sample equity curve for charting.
    """
    try:
        result = run_monte_carlo(request)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Simulation error: {e}")


@app.get("/system/logs")
async def get_system_logs():
    """Plain-text system report with live balance and recent log lines."""
    log_content = "\n".join(bot.logs)
    status = bot.get_status()
    acc    = status.get("account") or {"balance": 0, "equity": 0}

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
    """Public health check — no auth required."""
    return {"status": "ok", "version": "51.0", "mode": "paper"}

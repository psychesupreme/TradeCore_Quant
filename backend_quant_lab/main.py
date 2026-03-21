# ============================================================
# Kom v1.0 (formerly TradeCore) — main.py  
# [SPRINT 18: DUAL-LOOP ARCHITECTURE & API REBRAND]
#
# HISTORICAL ARCHITECTURE NOTES (Sprints 1 - 17c):
#   - [Sprint 1-10] The original engine ran on a pure `while True:` 
#     sleep loop, which blocked Telegram commands and API requests.
#   - [Sprint 11] Migrated to FastAPI + Uvicorn to allow the local 
#     HTML/Flutter dashboards to pull state asynchronously without 
#     interrupting the trade execution thread.
#   - [Sprint 14b] Offloaded DB historical sync to a separate 
#     subprocess (sync_db.py) to prevent GIL thread-locking.
#   - [Sprint 17b] Added APScheduler to replace crude `asyncio` 
#     sleep loops, ensuring exact execution timing and preventing 
#     memory leaks from overlapping async tasks.
#   - [BUG-54] Implemented Starlette CORSMiddleware to allow 
#     local dashboard.html fetch requests.
#
# SPRINT 18 UPGRADES (The Kom Transition):
#   [DECOUPLED SCHEDULING] The monolithic 60-second loop was destroying 
#   latency during heavy Pandas recalculations. It is now split:
#     1. run_execution_cycle: Runs every 10s (stops, momentum, scale-outs).
#     2. run_analysis_cycle: Runs every 60s (VWAP, Wyckoff, SMC math).
#   [VERSION CONTROL] System-wide rebrand to Kom v1.0.
#   [API RESTORATION] Endpoints mapped to /bot/* for Flutter compatibility.
# ============================================================

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.background import BackgroundScheduler
from bot_engine import TradingBot
import logging
from datetime import datetime
import subprocess
import os
import sys

# ==========================================
# LOGGING SETUP
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("Kom_API")

# ==========================================
# FASTAPI INITIALIZATION
# ==========================================
app = FastAPI(title="Kom API", version="1.0")

# [BUG-54 FIX] Add CORS middleware to allow the local dashboard.html 
# to fetch data without being blocked by the browser's security policy.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Localhost access permitted
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize the Master Engine
bot = TradingBot()
scheduler = BackgroundScheduler()

def run_sync_db():
    """
    [SPRINT 14b] Runs the DB sync script via subprocess.
    Prevents the heavy SQLite historical sync from blocking the 
    Fast/Heavy execution loops in the main thread.
    """
    try:
        if os.path.exists("sync_db.py"):
            subprocess.Popen([sys.executable, "sync_db.py"])
        else:
            logger.warning("sync_db.py not found. Skipping historical sync.")
    except Exception as e:
        logger.error(f"Sync DB Error: {e}")

@app.on_event("startup")
def startup_event():
    logger.info("==================================================")
    logger.info("🚀 System Startup: Initializing Kom v1.0...")
    logger.info("==================================================")
    
    # Boot the MT5 connection and internal state
    if not bot.start_service():
        logger.error("❌ Kom Engine failed to start. Check MT5 connection.")
        return
        
    # ==========================================
    # SPRINT 18: ASYNCHRONOUS DUAL-LOOP ROUTING
    # ==========================================
    
    # 1. The Execution Loop (Fast: 10s)
    # High-frequency management: Trailing stops, scale-outs, momentum guards
    scheduler.add_job(
        bot.run_execution_cycle, 
        'interval', 
        seconds=10, 
        id='execution_loop', 
        replace_existing=True,
        max_instances=1 # Prevents thread pile-up if MT5 hangs
    )
    
    # 2. The Analysis Loop (Heavy: 60s)
    # Low-frequency structural math: Pandas, VWAP, SMC logic
    scheduler.add_job(
        bot.run_analysis_cycle, 
        'interval', 
        seconds=60, 
        id='analysis_loop', 
        replace_existing=True,
        max_instances=1
    )
    
    # 3. Database Synchronization (Safety Net: 5m)
    scheduler.add_job(
        run_sync_db, 
        'interval', 
        minutes=5, 
        id='db_sync', 
        replace_existing=True,
        max_instances=1
    )
    
    # 4. Daily Summary Telegram Report
    scheduler.add_job(
        bot.send_daily_summary, 
        'cron', 
        hour=23, 
        minute=50, 
        id='daily_summary', 
        replace_existing=True
    )
    
    scheduler.start()
    logger.info("✅ Scheduler Active: Dual-Loop (Execution 10s / Analysis 60s) Online.")

@app.on_event("shutdown")
def shutdown_event():
    logger.info("🛑 Shutting down Kom v1.0...")
    scheduler.shutdown()
    bot.stop_service()
    logger.info("✅ System safely offline.")

# ==========================================
# RESTORED DASHBOARD API ENDPOINTS
# ==========================================

@app.get("/")
def read_root():
    return {
        "system": "Kom",
        "version": "1.0",
        "status": "Online" if bot.is_running else "Offline"
    }

@app.get("/bot/status")
def get_status():
    return bot.get_status()

@app.get("/bot/performance")
def get_performance():
    return bot.get_performance()

@app.get("/bot/risk")
def get_risk():
    return bot.get_risk()

@app.get("/bot/news")
def get_news():
    return bot.get_news()

@app.get("/bot/pending")
def get_pending():
    try:
        import MetaTrader5 as mt5
        orders = mt5.orders_get()
        if not orders:
            return []
        result = []
        for o in orders:
            state = bot._pending_order_info.get(str(o.ticket), {})
            result.append({
                "ticket":     o.ticket,
                "symbol":     o.symbol,
                "type":       "BUY_LIMIT" if o.type == mt5.ORDER_TYPE_BUY_LIMIT else "SELL_LIMIT",
                "price_open": round(o.price_open, 5),
                "sl":         round(o.sl, 5),
                "tp":         round(o.tp, 5),
                "volume":     o.volume_current,
                "time_setup": o.time_setup,
                "confidence": state.get("confidence"),
                "regime":     state.get("regime", ""),
            })
        return result
    except Exception:
        return []


@app.get("/quant/status")
def get_quant_status():
    import sqlite3, json as _json, os
    result = {}
    try:
        result = bot.quant_engine.get_live_risk_params()
    except Exception:
        pass
    # Signal funnel
    try:
        con = sqlite3.connect("tradecore.db")
        rows = con.execute("SELECT result, COUNT(*) FROM signals GROUP BY result").fetchall()
        con.close()
        funnel = {r[0]: r[1] for r in rows}
        result['signal_funnel'] = {
            'filled':           funnel.get('FILLED', 0),
            'executed':         funnel.get('EXECUTED', 0),
            'attempted':        funnel.get('ATTEMPTED', 0),
            'orphaned_pre_s20': funnel.get('ORPHANED_PRE_S20', 0),
            'low_confidence':   sum(v for k, v in funnel.items() if 'LOW_CONFIDENCE' in k),
            'h4_blocked':       funnel.get('H4_BLOCKED', 0),
            'cooldown':         funnel.get('COOLDOWN', 0),
            'rejected':         sum(v for k, v in funnel.items() if 'REJECTED' in k),
            'skipped':          funnel.get('SKIPPED', 0),
        }
    except Exception:
        result['signal_funnel'] = {}
    # ML model metadata
    try:
        meta_path = os.path.join("media", "kom_xgboost_v1_meta.json")
        if os.path.exists(meta_path):
            with open(meta_path) as f:
                meta = _json.load(f)
            result['ml_model'] = {
                'active':       meta.get('precision_gate') == 'ACTIVE',
                'precision':    round(meta.get('cv_precision', 0) * 100, 1),
                'accuracy':     round(meta.get('cv_accuracy', 0) * 100, 1),
                'n_trained_on': meta.get('n_trades', 0),
                'trained_at':   meta.get('trained_at', '')[:16],
                'gate':         meta.get('precision_gate', 'DISABLED'),
            }
        else:
            result['ml_model'] = {'active': False, 'gate': 'NOT_TRAINED'}
    except Exception:
        result['ml_model'] = {'active': False, 'gate': 'ERROR'}
    return result


@app.get("/quant/export_report")
def export_report():
    import sqlite3, io, csv
    from fastapi.responses import StreamingResponse
    from datetime import datetime as _dt
    try:
        con = sqlite3.connect("tradecore.db")
        rows = con.execute("""
            SELECT ticket, symbol, type, volume, open_price, close_price,
                   profit, open_time, close_time, account_id, model_type, regime
            FROM trades
            WHERE (comment IS NULL OR comment NOT LIKE '%ghost%')
              AND profit IS NOT NULL
            ORDER BY open_time ASC
        """).fetchall()
        con.close()
        cols = ['ticket','symbol','type','volume','open_price','close_price',
                'profit','open_time','close_time','account_id','model_type','regime']
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(cols)
        writer.writerows(rows)
        buf.seek(0)
        fname = f"kom_audit_{_dt.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
        return StreamingResponse(
            io.BytesIO(buf.getvalue().encode()),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={fname}"}
        )
    except Exception as e:
        return {"error": str(e)}

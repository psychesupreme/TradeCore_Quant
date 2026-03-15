# ============================================================
# Kom v1.0 — main.py  [SPRINT 18: DUAL-LOOP ARCHITECTURE]
#
# SPRINT 18 UPGRADES:
#   [DECOUPLED SCHEDULING] The monolithic 60-second loop is split:
#     - run_execution_cycle: Runs every 10 seconds. Manages live 
#       trailing stops, Take Profits, and momentum kill-switches.
#     - run_analysis_cycle: Runs every 60 seconds. Handles the heavy
#       Pandas lifting (VWAP, Wyckoff, SMC) to find setups.
#   [VERSION CONTROL] System-wide rebrand to Kom v1.0.
# ============================================================

from fastapi import FastAPI, BackgroundTasks
from apscheduler.schedulers.background import BackgroundScheduler
from bot_engine import TradingBot
import logging
from datetime import datetime
import subprocess
import os
import sys

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("Kom_API")

# Initialize FastAPI with new Version Control
app = FastAPI(title="Kom API", version="1.0")

# Initialize the Master Engine
bot = TradingBot()
scheduler = BackgroundScheduler()

def run_sync_db():
    """Runs the DB sync script via subprocess to avoid blocking the Fast/Heavy loops."""
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
        max_instances=1
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
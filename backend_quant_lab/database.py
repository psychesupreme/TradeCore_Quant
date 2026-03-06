# File: backend_quant_lab/database.py

import sqlite3
from datetime import datetime

DB_NAME = "tradecore.db"

def get_db_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row  # Allows accessing columns by name
    return conn

def init_db():
    """Initializes the database tables if they don't exist"""
    conn = get_db_connection()
    c = conn.cursor()
    
    # 1. TRADES TABLE
    # [SCHEMA FIX] Added slippage column — was in live DB via migration but
    # missing from init_db(), so a fresh install would create an incomplete schema.
    c.execute('''
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket INTEGER UNIQUE,
            symbol TEXT,
            type TEXT,
            volume REAL,
            open_price REAL,
            sl REAL,
            tp REAL,
            open_time DATETIME,
            close_price REAL,
            close_time DATETIME,
            profit REAL,
            commission REAL,
            comment TEXT,
            slippage REAL
        )
    ''')

    # 2. SIGNALS TABLE (The Brain's History)
    # [SCHEMA FIX] Added outcome, pips_result, hold_duration_min — present in live
    # DB via migration but absent here. Also: result column comment was truncated.
    c.execute('''
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            signal_type TEXT,
            confidence REAL,
            indicators TEXT,
            result TEXT,
            outcome TEXT,
            pips_result REAL,
            hold_duration_min REAL
        )
    ''')

    # 3. ACCOUNT SNAPSHOTS (The Equity Curve)
    # [SCHEMA FIX] Added margin_level column — present in live DB via migration
    # but absent here. log_snapshot() was writing margin_level into the wrong
    # 'margin' column as a result.
    c.execute('''
        CREATE TABLE IF NOT EXISTS account_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            balance REAL,
            equity REAL,
            margin REAL,
            free_margin REAL,
            margin_level REAL
        )
    ''')

    conn.commit()
    conn.close()
    print("✅ Database System: Online & Ready.")

# Run initialization immediately on import
init_db()
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
    
    # 1. TRADES TABLE (The Ledger)
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
            comment TEXT
        )
    ''')

    # 2. SIGNALS TABLE (The Brain's History)
    # This stores WHY we took a trade, crucial for AI training later.
    c.execute('''
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            signal_type TEXT,
            confidence REAL,
            indicators TEXT, -- JSON string of RSI, MA values
            result TEXT -- 'EXECUTED', 'SKIPPED', 'REJECTED'
        )
    ''')
    
    # 3. ACCOUNT SNAPSHOTS (The Equity Curve)
    c.execute('''
        CREATE TABLE IF NOT EXISTS account_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            balance REAL,
            equity REAL,
            margin REAL,
            free_margin REAL
        )
    ''')

    conn.commit()
    conn.close()
    print("✅ Database System: Online & Ready.")

# Run initialization immediately on import
init_db()
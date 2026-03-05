# ============================================================
# TradeCore v51.0 — database.py
# SPRINT 2 FIXES APPLIED:
#   [BUG-08b] account_snapshots.margin column renamed to
#             margin_level to reflect actual content (a %).
#   [FORWARD] trades table gains 'slippage' and 'commission'
#             columns — populated by Sprint 3 execute_signal.
#   [FORWARD] signals table gains 'outcome', 'pips_result',
#             'hold_duration_min' for ML training readiness.
#             These are empty now and filled as trades close.
#
# MIGRATION NOTE:
#   If tradecore.db already exists, run migrate_schema() once
#   to add the new columns without losing existing data.
#   migrate_schema() is safe to call repeatedly (uses
#   ALTER TABLE ... ADD COLUMN which is idempotent via try/except).
# ============================================================

import sqlite3
from datetime import datetime

DB_NAME = "tradecore.db"


def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """
    Creates all tables if they don't exist.
    Safe to call on every startup — IF NOT EXISTS guards all DDL.
    """
    conn = get_db_connection()
    c = conn.cursor()

    # ── 1. TRADES TABLE ────────────────────────────────────
    # The primary ledger. save_trade() writes here on every
    # successful execution. sync_db.py closes entries when
    # MT5 confirms the position has been closed.
    c.execute('''
        CREATE TABLE IF NOT EXISTS trades (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket       INTEGER UNIQUE,
            symbol       TEXT,
            type         TEXT,
            volume       REAL,
            open_price   REAL,
            sl           REAL,
            tp           REAL,
            open_time    DATETIME,
            close_price  REAL,
            close_time   DATETIME,
            profit       REAL,
            slippage     REAL,        -- entry price deviation (points)
            commission   REAL,        -- broker commission if reported
            comment      TEXT
        )
    ''')

    # ── 2. SIGNALS TABLE ───────────────────────────────────
    # Records every signal evaluation — executed, skipped, or
    # rejected. The outcome/pips fields are filled retroactively
    # by sync_db when the corresponding trade closes.
    # This is the training dataset for future ML work.
    c.execute('''
        CREATE TABLE IF NOT EXISTS signals (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol            TEXT,
            timestamp         DATETIME DEFAULT CURRENT_TIMESTAMP,
            signal_type       TEXT,
            confidence        REAL,
            indicators        TEXT,      -- JSON: vol ratio, ATR, regime, etc.
            result            TEXT,      -- EXECUTED | SKIPPED | REJECTED
            outcome           TEXT,      -- WIN | LOSS | BE | NULL (filled later)
            pips_result       REAL,      -- signed pip result (filled later)
            hold_duration_min INTEGER    -- minutes held (filled later)
        )
    ''')

    # ── 3. ACCOUNT SNAPSHOTS ───────────────────────────────
    # Equity curve data. One row per run_cycle() invocation.
    # [BUG-08b FIX] Column renamed from 'margin' to 'margin_level'
    # to correctly reflect that it stores a percentage (e.g. 1250.5)
    # not a dollar amount.
    c.execute('''
        CREATE TABLE IF NOT EXISTS account_snapshots (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp    DATETIME DEFAULT CURRENT_TIMESTAMP,
            balance      REAL,
            equity       REAL,
            margin_level REAL,     -- percentage (was incorrectly named 'margin')
            free_margin  REAL
        )
    ''')

    conn.commit()
    conn.close()
    print("✅ Database System: Online & Ready.")


def migrate_schema():
    """
    Adds new columns to an existing database without dropping any data.
    Safe to call repeatedly — each ALTER is wrapped in its own try/except.
    Run this once after deploying Sprint 2 if tradecore.db already exists.
    """
    conn = get_db_connection()
    c = conn.cursor()
    migrations = [
        # trades — new columns
        "ALTER TABLE trades ADD COLUMN slippage REAL",
        "ALTER TABLE trades ADD COLUMN commission REAL",
        # signals — outcome tracking for ML
        "ALTER TABLE signals ADD COLUMN outcome TEXT",
        "ALTER TABLE signals ADD COLUMN pips_result REAL",
        "ALTER TABLE signals ADD COLUMN hold_duration_min INTEGER",
        # account_snapshots — rename workaround (SQLite can't rename columns before 3.25)
        # We add margin_level as a new column; the old 'margin' column stays but is ignored.
        "ALTER TABLE account_snapshots ADD COLUMN margin_level REAL",
    ]
    applied = 0
    for sql in migrations:
        try:
            c.execute(sql)
            applied += 1
        except Exception:
            pass  # Column already exists — skip
    conn.commit()
    conn.close()
    if applied > 0:
        print(f"✅ DB Migration: {applied} schema change(s) applied.")
    else:
        print("✅ DB Migration: Schema already up to date.")


# Run initialization and migration on import
init_db()
migrate_schema()

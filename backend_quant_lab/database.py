# ============================================================
# TradeCore v53.0 — database.py  [SPRINT 8]
#
# SPRINT 7 ADDITIONS (preserved):
#   trades: mae, mfe, return_pct, regime
#   signals: ict_score, kill_zone, ict_conditions
#   account_snapshots: margin_level
#
# SPRINT 8 ADDITIONS:
#   All tables: account_id TEXT — MT5 login number.
#     Enables complete isolation between demo and live accounts,
#     or between multiple MT5 accounts on the same server.
#     Indexed for fast per-account queries.
#
#   signals: model_type TEXT — which model fired the signal
#            (ICT_STANDARD | ASIAN_RANGE | PDH_PDL | FVG_FILL |
#             ORB | JUDAS_SWING | WEEKEND_TRAP | HTF_FVG)
#            model_sizing TEXT — STANDARD | MICRO | NANO
#            Enables independent win-rate tracking per model.
# ============================================================

import sqlite3
from datetime import datetime

DB_NAME = "tradecore.db"


def get_db_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    # WAL mode: multiple readers, one writer, no lock contention
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _safe_add_column(cursor, table: str, column: str, col_type: str):
    """
    Adds a column only if it doesn't already exist.
    SQLite does not support IF NOT EXISTS on ALTER TABLE,
    so we check PRAGMA table_info first.
    """
    cursor.execute(f"PRAGMA table_info({table})")
    existing = [row[1] for row in cursor.fetchall()]
    if column not in existing:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
        print(f"  ✅ Schema: Added {table}.{column} ({col_type})")


def init_db():
    """
    Initialises all tables and runs the Sprint 7 schema migration.
    Safe to call on every boot — existing data is never touched.
    """
    conn = get_db_connection()
    c = conn.cursor()

    # ── 1. TRADES TABLE ────────────────────────────────────────
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
            commission   REAL,
            comment      TEXT,
            slippage     REAL,
            -- Sprint 7 additions
            mae          REAL,
            mfe          REAL,
            return_pct   REAL,
            regime       TEXT,
            -- Sprint 8 additions
            account_id   TEXT,
            model_type   TEXT,
            model_sizing TEXT
        )
    ''')

    # Sprint 7+8: migrate existing trades table (safe for live DB)
    for col, typ in [
        ("mae",        "REAL"),
        ("mfe",        "REAL"),
        ("return_pct", "REAL"),
        ("regime",     "TEXT"),
        ("slippage",   "REAL"),
        ("account_id",   "TEXT"),    # Sprint 8
        ("model_type",   "TEXT"),    # Sprint 8 — which secondary model fired
        ("model_sizing", "TEXT"),    # Sprint 8 — STANDARD | MICRO | NANO
    ]:
        _safe_add_column(c, "trades", col, typ)

    # ── 2. SIGNALS TABLE ───────────────────────────────────────
    c.execute('''
        CREATE TABLE IF NOT EXISTS signals (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol         TEXT,
            timestamp      DATETIME DEFAULT CURRENT_TIMESTAMP,
            signal_type    TEXT,
            confidence     REAL,
            indicators     TEXT,
            result         TEXT,
            outcome        TEXT,
            pips_result    REAL,
            hold_duration_min REAL,
            -- Sprint 7 additions
            ict_score      REAL,
            kill_zone      TEXT,
            ict_conditions TEXT,
            -- Sprint 8 additions
            model_type     TEXT,
            model_sizing   TEXT,
            account_id     TEXT
        )
    ''')

    for col, typ in [
        ("ict_score",       "REAL"),
        ("kill_zone",       "TEXT"),
        ("ict_conditions",  "TEXT"),
        ("model_type",      "TEXT"),    # Sprint 8
        ("model_sizing",    "TEXT"),    # Sprint 8
        ("account_id",      "TEXT"),    # Sprint 8
    ]:
        _safe_add_column(c, "signals", col, typ)

    # ── 3. ACCOUNT SNAPSHOTS ───────────────────────────────────
    c.execute('''
        CREATE TABLE IF NOT EXISTS account_snapshots (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp    DATETIME DEFAULT CURRENT_TIMESTAMP,
            balance      REAL,
            equity       REAL,
            margin       REAL,
            free_margin  REAL,
            margin_level REAL,
            account_id   TEXT    -- Sprint 8: binds snapshot to MT5 login
        )
    ''')

    _safe_add_column(c, "account_snapshots", "margin_level", "REAL")
    _safe_add_column(c, "account_snapshots", "account_id", "TEXT")   # Sprint 8

    # Sprint 8: index account_id on all tables for fast per-account filtering
    for table in ["trades", "signals", "account_snapshots"]:
        try:
            c.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_account "
                      f"ON {table}(account_id)")
        except Exception:
            pass

    conn.commit()
    conn.close()
    print("✅ Database System: Online & Ready.")


# Run on import
init_db()

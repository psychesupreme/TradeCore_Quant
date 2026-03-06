# File: backend_quant_lab/db_manager.py
# ============================================================
# SPRINT 5 FIXES:
#   [BUG-22] Connection pooling — replaced per-call open/close with a
#            thread-local persistent connection + WAL journal mode.
#            Previous pattern: 17 assets x 60s loop = 1020 connect/close
#            cycles per hour. WAL allows concurrent reads during writes.
#   [BUG-26] log_snapshot() had wrong column mapping — the third positional
#            arg was margin_level but the INSERT mapped it to the 'margin'
#            column (actual margin usage in $). The 'margin_level' column
#            (the % ratio) was never written, 'margin' was always NULL.
# ============================================================

import threading
import json
import sqlite3
from database import DB_NAME

# ── THREAD-LOCAL CONNECTION POOL ─────────────────────────────────────────────
# Each thread (main, APScheduler, FastAPI worker) gets its own persistent
# SQLite connection. WAL mode lets readers and the writer run concurrently,
# eliminating "database is locked" errors under the 60s trade loop + 5min
# DB sync + FastAPI request concurrency pattern.
_thread_local = threading.local()

def _get_conn():
    """Returns this thread's persistent WAL-mode SQLite connection."""
    conn = getattr(_thread_local, 'conn', None)
    if conn is None:
        conn = sqlite3.connect(DB_NAME, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")      # concurrent read+write
        conn.execute("PRAGMA synchronous=NORMAL")    # safe + fast
        _thread_local.conn = conn
    return conn


class DBManager:

    @staticmethod
    def log_signal(symbol, signal_type, confidence, indicators_dict, result):
        conn = _get_conn()
        try:
            conn.execute(
                'INSERT INTO signals (symbol, signal_type, confidence, indicators, result) VALUES (?, ?, ?, ?, ?)',
                (symbol, signal_type, confidence, json.dumps(indicators_dict), result)
            )
            conn.commit()
        except Exception as e:
            print(f"⚠️ DB Error (Signal): {e}")

    @staticmethod
    def log_snapshot(balance, equity, margin_level, free_margin):
        """
        [BUG-26 FIX] Third parameter is margin_level (the % ratio, e.g. 12031%).
        The old INSERT positionally mapped it to the 'margin' column (raw $ usage)
        which is a different metric — so 'margin' was always NULL and 'margin_level'
        was never recorded. Now writes each value to its correct named column.
        The 'margin' column (raw margin in $) is omitted as it's not currently
        returned by get_account_info() — NULL is correct here until that's added.
        """
        conn = _get_conn()
        try:
            conn.execute(
                '''INSERT INTO account_snapshots (balance, equity, free_margin, margin_level)
                   VALUES (?, ?, ?, ?)''',
                (balance, equity, free_margin, margin_level)
            )
            conn.commit()
        except Exception as e:
            print(f"⚠️ DB Error (Snapshot): {e}")

    @staticmethod
    def save_trade(ticket, symbol, type_op, vol, open_price, sl, tp, time, slippage=None):
        """
        Upsert: inserts a new trade record, silently ignores duplicate tickets.
        slippage is optional — populated on execution when the broker confirms fill.
        """
        conn = _get_conn()
        try:
            conn.execute(
                '''INSERT OR IGNORE INTO trades
                   (ticket, symbol, type, volume, open_price, sl, tp, open_time, slippage)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (ticket, symbol, type_op, vol, open_price, sl, tp, time, slippage)
            )
            conn.commit()
        except Exception as e:
            print(f"⚠️ DB Error (Trade): {e}")

    @staticmethod
    def update_signal_result(symbol, signal_type, new_result):
        """
        [BUG-33 FIX] Updates the most recent ATTEMPTED signal for a symbol
        to its actual MT5 outcome: FILLED or REJECTED:<reason>.
        
        Called from execute_signal's async callback once MT5 responds,
        so the signals table accurately reflects what the broker accepted
        rather than just what was submitted.  Scoped to last 2 minutes to
        avoid touching historical records under concurrent load.
        """
        conn = _get_conn()
        try:
            conn.execute(
                '''UPDATE signals
                   SET result = ?
                   WHERE id = (
                       SELECT id FROM signals
                       WHERE symbol      = ?
                         AND signal_type = ?
                         AND result      = 'ATTEMPTED'
                         AND timestamp  >= datetime('now', '-2 minutes')
                       ORDER BY timestamp DESC
                       LIMIT 1
                   )''',
                (new_result, symbol, signal_type)
            )
            conn.commit()
        except Exception as e:
            print(f"⚠️ DB Error (Signal Update): {e}")

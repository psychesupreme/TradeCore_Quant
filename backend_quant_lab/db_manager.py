# ============================================================
# TradeCore v51.0 — db_manager.py
# SPRINT 2 FIXES APPLIED:
#   [BUG-08b] log_snapshot() now writes to 'margin_level'
#             column instead of the misnamed 'margin' column.
#   [QUALITY] Thread lock prevents concurrent write collisions
#             when multiple signals fire simultaneously.
#   [QUALITY] save_trade() now accepts optional slippage arg
#             for execution quality tracking (Sprint 3 passes it).
#   [QUALITY] update_trade_close() added for sync_db.py to use
#             instead of raw SQL — centralises all DB writes here.
#   [FORWARD] update_signal_outcome() prepares for ML training:
#             writes WIN/LOSS/BE and pip result when trade closes.
# ============================================================

import threading
import json
from database import get_db_connection

# Single write lock for the module.
# All writes acquire this before opening a connection.
# Reads are not locked — SQLite handles concurrent reads fine.
_write_lock = threading.Lock()


class DBManager:

    # ──────────────────────────────────────────────────────
    # SIGNAL LOGGING
    # ──────────────────────────────────────────────────────
    @staticmethod
    def log_signal(symbol: str, signal_type: str, confidence: float,
                   indicators_dict: dict, result: str):
        """Records every signal evaluation: executed, skipped, or rejected."""
        with _write_lock:
            conn = get_db_connection()
            try:
                conn.execute('''
                    INSERT INTO signals (symbol, signal_type, confidence, indicators, result)
                    VALUES (?, ?, ?, ?, ?)
                ''', (symbol, signal_type, confidence, json.dumps(indicators_dict), result))
                conn.commit()
            except Exception as e:
                print(f"⚠️  DB Error (Signal): {e}")
            finally:
                conn.close()

    # ──────────────────────────────────────────────────────
    # ACCOUNT SNAPSHOT
    # ──────────────────────────────────────────────────────
    @staticmethod
    def log_snapshot(balance: float, equity: float, margin_level: float, free_margin: float):
        """
        Writes one equity curve data point per trading cycle.
        [BUG-08b FIX] Third parameter is margin_level (a %) not margin ($).
        Column name updated to match — schema migration in database.py handles
        existing databases.
        """
        with _write_lock:
            conn = get_db_connection()
            try:
                conn.execute('''
                    INSERT INTO account_snapshots (balance, equity, margin_level, free_margin)
                    VALUES (?, ?, ?, ?)
                ''', (balance, equity, margin_level, free_margin))
                conn.commit()
            except Exception as e:
                print(f"⚠️  DB Error (Snapshot): {e}")
            finally:
                conn.close()

    # ──────────────────────────────────────────────────────
    # TRADE OPEN
    # ──────────────────────────────────────────────────────
    @staticmethod
    def save_trade(ticket: int, symbol: str, type_op: str, vol: float,
                   open_price: float, sl: float, tp: float, open_time,
                   slippage: float = 0.0, comment: str = ""):
        """
        Records a newly opened trade.
        Called by execute_signal() in bot_engine immediately after
        TRADE_RETCODE_DONE is confirmed.

        slippage: difference between requested and filled price in price units.
                  Used for execution quality analysis and broker auditing.
        """
        with _write_lock:
            conn = get_db_connection()
            try:
                conn.execute('''
                    INSERT OR IGNORE INTO trades
                        (ticket, symbol, type, volume, open_price, sl, tp,
                         open_time, slippage, comment)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (ticket, symbol, type_op, float(vol), float(open_price),
                      float(sl), float(tp), open_time,
                      float(slippage), comment))
                conn.commit()
            except Exception as e:
                print(f"⚠️  DB Error (Trade Open): {e}")
            finally:
                conn.close()

    # ──────────────────────────────────────────────────────
    # TRADE CLOSE
    # ──────────────────────────────────────────────────────
    @staticmethod
    def update_trade_close(ticket: int, close_price: float,
                           close_time, profit: float):
        """
        Called by sync_db.py when a trade is confirmed closed in MT5.
        Writes the exit data and realized profit to the ledger.
        """
        with _write_lock:
            conn = get_db_connection()
            try:
                conn.execute('''
                    UPDATE trades
                    SET close_price = ?,
                        close_time  = ?,
                        profit      = ?
                    WHERE ticket = ?
                ''', (float(close_price), close_time, float(profit), ticket))
                conn.commit()
            except Exception as e:
                print(f"⚠️  DB Error (Trade Close): {e}")
            finally:
                conn.close()

    # ──────────────────────────────────────────────────────
    # SIGNAL OUTCOME UPDATE (ML Training Data)
    # ──────────────────────────────────────────────────────
    @staticmethod
    def update_signal_outcome(symbol: str, signal_timestamp,
                               outcome: str, pips_result: float,
                               hold_duration_min: int):
        """
        Links a closed trade result back to the original signal record.
        Matches by symbol and proximity to the signal timestamp.
        outcome: 'WIN' | 'LOSS' | 'BE' (breakeven, within 1 pip)

        Called by sync_db.py after each trade close is confirmed.
        This builds the ML training dataset over time.
        """
        with _write_lock:
            conn = get_db_connection()
            try:
                conn.execute('''
                    UPDATE signals
                    SET outcome           = ?,
                        pips_result       = ?,
                        hold_duration_min = ?
                    WHERE symbol = ?
                      AND result = 'EXECUTED'
                      AND outcome IS NULL
                      AND ABS(julianday(timestamp) - julianday(?)) < 0.01
                ''', (outcome, float(pips_result), hold_duration_min,
                      symbol, signal_timestamp))
                conn.commit()
            except Exception as e:
                print(f"⚠️  DB Error (Signal Outcome): {e}")
            finally:
                conn.close()

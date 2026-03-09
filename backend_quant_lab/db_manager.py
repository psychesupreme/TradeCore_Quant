# ============================================================
# TradeCore v52.0 — db_manager.py  [SPRINT 7]
#
# SPRINT 7 ADDITIONS:
#   update_mae_mfe()       — Called every cycle for open trades.
#   save_trade()           — Now accepts regime.
#   log_signal()           — Now accepts ict_score, kill_zone, ict_conditions.
#   log_snapshot()         — Accepts margin_level (already live, kept).
#   update_signal_result() — BUG-33 fix preserved.
#   get_closed_trades()    — New: DataFrame for quant engine.
#   get_equity_curve()     — New: balance series for ratio math.
#   get_signal_history()   — New: signal log for QML.
# ============================================================

from database import get_db_connection
import json
import numpy as np
import pandas as pd
from datetime import datetime, timedelta


class _SafeEncoder(json.JSONEncoder):
    """
    Handles numpy scalar types that standard json.dumps rejects.
    numpy.bool_ is the most common culprit — comes from pandas/numpy
    boolean operations in the ICT confluence scorer.
    """
    def default(self, obj):
        if isinstance(obj, np.bool_):    return bool(obj)
        if isinstance(obj, np.integer):  return int(obj)
        if isinstance(obj, np.floating): return float(obj)
        if isinstance(obj, np.ndarray):  return obj.tolist()
        return super().default(obj)


class DBManager:

    # ─────────────────────────────────────────────────────────
    # SIGNAL LOGGING
    # ─────────────────────────────────────────────────────────

    @staticmethod
    def log_signal(symbol, signal_type, confidence, indicators_dict, result,
                   ict_score=None, kill_zone=None, ict_conditions=None,
                   model_type=None, model_sizing=None, account_id=None):
        """[SPRINT 8] model_type/model_sizing enable per-model performance tracking.
        account_id isolates records across demo/live account switches."""
        conn = get_db_connection()
        c = conn.cursor()
        try:
            ict_cond_json = json.dumps(ict_conditions, cls=_SafeEncoder) if ict_conditions else None
            c.execute('''
                INSERT INTO signals
                    (symbol, signal_type, confidence, indicators,
                     result, ict_score, kill_zone, ict_conditions,
                     model_type, model_sizing, account_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (symbol, signal_type, confidence,
                  json.dumps(indicators_dict, cls=_SafeEncoder), result,
                  ict_score, kill_zone, ict_cond_json,
                  model_type, model_sizing, account_id))
            conn.commit()
        except Exception as e:
            print(f"⚠️ DB Error (Signal): {e}")
        finally:
            conn.close()

    @staticmethod
    def update_signal_result(symbol, signal_type, new_result):
        """[BUG-33 FIX] Updates ATTEMPTED → FILLED or REJECTED once MT5 responds."""
        conn = get_db_connection()
        c = conn.cursor()
        try:
            two_min_ago = (datetime.utcnow() - timedelta(minutes=2)).strftime('%Y-%m-%d %H:%M:%S')
            c.execute('''
                UPDATE signals SET result = ?
                WHERE id = (
                    SELECT id FROM signals
                    WHERE symbol = ? AND signal_type = ?
                      AND result = 'ATTEMPTED' AND timestamp >= ?
                    ORDER BY timestamp DESC LIMIT 1
                )
            ''', (new_result, symbol, signal_type, two_min_ago))
            conn.commit()
        except Exception as e:
            print(f"⚠️ DB Error (Signal Update): {e}")
        finally:
            conn.close()

    # ─────────────────────────────────────────────────────────
    # TRADE LOGGING
    # ─────────────────────────────────────────────────────────

    @staticmethod
    def save_trade(ticket, symbol, type_op, vol, open_price, sl, tp, time,
                   regime=None, account_id=None, model_type=None, model_sizing=None):
        """Opens a new trade record. [SPRINT 8] account_id + model tracking."""
        conn = get_db_connection()
        c = conn.cursor()
        try:
            c.execute('''
                INSERT OR IGNORE INTO trades
                    (ticket, symbol, type, volume, open_price, sl, tp,
                     open_time, regime, mae, mfe, account_id, model_type, model_sizing)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0.0, 0.0, ?, ?, ?)
            ''', (ticket, symbol, type_op, vol, open_price, sl, tp, time, regime,
                  account_id, model_type, model_sizing))
            conn.commit()
        except Exception as e:
            print(f"⚠️ DB Error (Trade Open): {e}")
        finally:
            conn.close()

    @staticmethod
    def update_mae_mfe(ticket, adverse_excursion, favorable_excursion):
        """
        [SPRINT 7] Called every cycle for each open position.
        adverse_excursion  = abs(open_price - worst_price_seen)  [always positive]
        favorable_excursion = abs(best_price_seen - open_price)  [always positive]
        MAX() ensures we only store the worst/best extremes, never override with
        a smaller value from a later tick.
        """
        conn = get_db_connection()
        c = conn.cursor()
        try:
            c.execute('''
                UPDATE trades
                SET mae = MAX(COALESCE(mae, 0.0), ?),
                    mfe = MAX(COALESCE(mfe, 0.0), ?)
                WHERE ticket = ? AND close_time IS NULL
            ''', (adverse_excursion, favorable_excursion, ticket))
            conn.commit()
        except Exception as e:
            print(f"⚠️ DB Error (MAE/MFE): {e}")
        finally:
            conn.close()

    @staticmethod
    def close_trade(ticket, close_price, close_time, profit,
                    commission=0.0, slippage=0.0):
        conn = get_db_connection()
        c = conn.cursor()
        try:
            c.execute("SELECT open_price, volume FROM trades WHERE ticket = ?", (ticket,))
            row = c.fetchone()
            return_pct = None
            if row and row[0] and row[1]:
                approx_risk = row[0] * row[1] * 0.02
                return_pct  = round(profit / approx_risk, 4) if approx_risk else None
            c.execute('''
                UPDATE trades
                SET close_price=?, close_time=?, profit=?,
                    commission=?, slippage=?, return_pct=?
                WHERE ticket=?
            ''', (close_price, close_time, profit,
                  commission, slippage, return_pct, ticket))
            conn.commit()
        except Exception as e:
            print(f"⚠️ DB Error (Trade Close): {e}")
        finally:
            conn.close()

    # ─────────────────────────────────────────────────────────
    # ACCOUNT SNAPSHOTS
    # ─────────────────────────────────────────────────────────

    @staticmethod
    def log_snapshot(balance, equity, margin_level, free_margin, margin=0.0,
                    account_id=None):
        """[SPRINT 8] account_id isolates equity curves across account switches."""
        conn = get_db_connection()
        c = conn.cursor()
        try:
            c.execute('''
                INSERT INTO account_snapshots
                    (balance, equity, margin, free_margin, margin_level, account_id)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (balance, equity, margin, free_margin, margin_level, account_id))
            conn.commit()
        except Exception as e:
            print(f"⚠️ DB Error (Snapshot): {e}")
        finally:
            conn.close()

    # ─────────────────────────────────────────────────────────
    # QUANT ENGINE DATA ACCESSORS
    # ─────────────────────────────────────────────────────────

    @staticmethod
    def get_closed_trades(account_id=None) -> pd.DataFrame:
        """Returns closed trades for the given account_id (or all if None).
        [SPRINT 8] account_id filter ensures quant metrics are not cross-contaminated
        between demo and live accounts."""
        conn = get_db_connection()
        try:
            if account_id:
                df = pd.read_sql_query('''
                    SELECT ticket, symbol, type, volume, open_price, close_price,
                           open_time, close_time, profit,
                           COALESCE(mae, 0.0) AS mae,
                           COALESCE(mfe, 0.0) AS mfe,
                           return_pct, regime, comment, model_type,
                           ROUND((julianday(close_time)-julianday(open_time))*1440,1) AS hold_min
                    FROM trades
                    WHERE close_time IS NOT NULL
                      AND comment != 'ghost_cleanup'
                      AND profit IS NOT NULL
                      AND (account_id = ? OR account_id IS NULL)
                    ORDER BY close_time ASC
                ''', conn, params=(account_id,))
            else:
                df = pd.read_sql_query('''
                    SELECT ticket, symbol, type, volume, open_price, close_price,
                           open_time, close_time, profit,
                           COALESCE(mae, 0.0) AS mae,
                           COALESCE(mfe, 0.0) AS mfe,
                           return_pct, regime, comment, model_type,
                           ROUND((julianday(close_time)-julianday(open_time))*1440,1) AS hold_min
                    FROM trades
                    WHERE close_time IS NOT NULL
                      AND comment != 'ghost_cleanup'
                      AND profit IS NOT NULL
                    ORDER BY close_time ASC
                ''', conn)
            df['open_time']  = pd.to_datetime(df['open_time'])
            df['close_time'] = pd.to_datetime(df['close_time'])
            return df
        except Exception as e:
            print(f"⚠️ DB Error (get_closed_trades): {e}")
            return pd.DataFrame()
        finally:
            conn.close()

    @staticmethod
    def get_equity_curve() -> pd.DataFrame:
        """Returns timestamped balance snapshots for Sharpe/Sortino/Calmar."""
        conn = get_db_connection()
        try:
            df = pd.read_sql_query('''
                SELECT timestamp, balance, equity
                FROM account_snapshots ORDER BY timestamp ASC
            ''', conn)
            df['timestamp'] = pd.to_datetime(df['timestamp'])
            return df
        except Exception as e:
            print(f"⚠️ DB Error (get_equity_curve): {e}")
            return pd.DataFrame()
        finally:
            conn.close()

    @staticmethod
    def get_open_trade_tickets() -> set:
        """
        Returns the set of tickets currently recorded as open (close_time IS NULL)
        in the DB. Used by bot_engine to detect MT5 fills that haven't been saved yet.
        """
        conn = get_db_connection()
        try:
            rows = conn.execute(
                "SELECT ticket FROM trades WHERE close_time IS NULL"
            ).fetchall()
            return {int(r[0]) for r in rows}
        except Exception as e:
            print(f"⚠️ DB Error (get_open_trade_tickets): {e}")
            return set()
        finally:
            conn.close()

    @staticmethod
    def get_signal_history() -> pd.DataFrame:
        """Returns signal log for QML training.
        [SPRINT 9] Filtered to FILLED/ATTEMPTED only — SKIPPED rows inflated
        the query to 48k+ rows and are irrelevant for model training."""
        conn = get_db_connection()
        try:
            df = pd.read_sql_query('''
                SELECT symbol, timestamp, signal_type, confidence,
                       result, ict_score, kill_zone, indicators
                FROM signals
                WHERE result IN ('FILLED', 'ATTEMPTED')
                ORDER BY timestamp ASC
            ''', conn)
            return df
        except Exception as e:
            print(f"⚠️ DB Error (get_signal_history): {e}")
            return pd.DataFrame()
        finally:
            conn.close()

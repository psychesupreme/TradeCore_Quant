# File: backend_quant_lab/db_manager.py

from database import get_db_connection
import json

class DBManager:
    @staticmethod
    def log_signal(symbol, signal_type, confidence, indicators_dict, result):
        conn = get_db_connection()
        c = conn.cursor()
        try:
            c.execute('''
                INSERT INTO signals (symbol, signal_type, confidence, indicators, result)
                VALUES (?, ?, ?, ?, ?)
            ''', (symbol, signal_type, confidence, json.dumps(indicators_dict), result))
            conn.commit()
        except Exception as e:
            print(f"⚠️ DB Error (Signal): {e}")
        finally:
            conn.close()

    @staticmethod
    def log_snapshot(balance, equity, margin, free_margin):
        conn = get_db_connection()
        c = conn.cursor()
        try:
            c.execute('''
                INSERT INTO account_snapshots (balance, equity, margin, free_margin)
                VALUES (?, ?, ?, ?)
            ''', (balance, equity, margin, free_margin))
            conn.commit()
        except Exception as e:
            print(f"⚠️ DB Error (Snapshot): {e}")
        finally:
            conn.close()

    @staticmethod
    def save_trade(ticket, symbol, type_op, vol, open_price, sl, tp, time):
        conn = get_db_connection()
        c = conn.cursor()
        try:
            # Upsert logic (Insert or Ignore if ticket exists)
            c.execute('''
                INSERT OR IGNORE INTO trades (ticket, symbol, type, volume, open_price, sl, tp, open_time)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (ticket, symbol, type_op, vol, open_price, sl, tp, time))
            conn.commit()
        except Exception as e:
            print(f"⚠️ DB Error (Trade): {e}")
        finally:
            conn.close()
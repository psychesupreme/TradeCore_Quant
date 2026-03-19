# ============================================================
# Kom v1.0 — backfill_signal_outcomes.py
# [SPRINT 21-C: SIGNAL OUTCOME BACKFILL]
#
# PURPOSE:
#   107 FILLED signals have no outcome label (WIN/LOSS/BREAK_EVEN)
#   because the close detection path in run_execution_cycle and
#   sync_db.py failed to record the corresponding trade close, so
#   update_signal_outcome() was never called.
#
#   This script matches each un-labelled FILLED signal to the
#   closest closed trade for the same symbol within a 4-hour
#   window, then writes outcome + pips_result.
#
#   SAFE TO RE-RUN: only touches signals WHERE outcome IS NULL.
#
# HOW TO RUN:
#   python backfill_signal_outcomes.py
#   Run once manually. No effect on running bot.
# ============================================================

import sqlite3
import pandas as pd
from datetime import datetime, timedelta

DB_PATH = "tradecore.db"


def backfill():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # All FILLED signals that never received an outcome
    unfilled = pd.read_sql_query("""
        SELECT id, symbol, timestamp, signal_type, confidence
        FROM signals
        WHERE result = 'FILLED' AND outcome IS NULL
        ORDER BY timestamp ASC
    """, conn)

    if unfilled.empty:
        print("✅ No signals need backfilling.")
        conn.close()
        return

    # All closed trades with valid data
    closed = pd.read_sql_query("""
        SELECT ticket, symbol, type, open_price, close_price,
               open_time, close_time, profit
        FROM trades
        WHERE close_time IS NOT NULL
          AND profit IS NOT NULL
          AND comment != 'ghost_cleanup'
    """, conn)

    unfilled['timestamp'] = pd.to_datetime(unfilled['timestamp'])
    closed['open_time']   = pd.to_datetime(closed['open_time'])
    closed['close_time']  = pd.to_datetime(closed['close_time'])

    cursor = conn.cursor()
    matched   = 0
    unmatched = 0

    for _, sig in unfilled.iterrows():
        sym        = sig['symbol']
        sig_time   = sig['timestamp']
        window_end = sig_time + timedelta(hours=4)

        # Find trades for this symbol that opened within 4h of the signal
        candidates = closed[
            (closed['symbol'] == sym) &
            (closed['open_time'] >= sig_time - timedelta(minutes=5)) &
            (closed['open_time'] <= window_end)
        ]

        if candidates.empty:
            unmatched += 1
            continue

        # Take the trade whose open_time is closest to the signal timestamp
        candidates = candidates.copy()
        candidates['dt'] = (candidates['open_time'] - sig_time).abs()
        trade = candidates.nsmallest(1, 'dt').iloc[0]

        profit    = float(trade['profit'])
        outcome   = "WIN" if profit > 0 else ("BREAK_EVEN" if profit == 0 else "LOSS")

        # pips_result: signed price delta in the direction of the trade
        direction   = 1 if trade['type'] == 'BUY' else -1
        pips_result = (float(trade['close_price']) - float(trade['open_price'])) * direction

        cursor.execute("""
            UPDATE signals
            SET outcome = ?, pips_result = ?
            WHERE id = ?
        """, (outcome, round(pips_result, 6), int(sig['id'])))

        matched += 1

    conn.commit()
    conn.close()

    print(f"✅ Backfill complete.")
    print(f"   Matched and updated : {matched}")
    print(f"   No trade found (>4h): {unmatched}")
    print(f"   Total processed     : {matched + unmatched}")


if __name__ == "__main__":
    print("--- 🔄 RUNNING SIGNAL OUTCOME BACKFILL ---")
    backfill()

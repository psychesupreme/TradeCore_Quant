# ============================================================
# Kom v1.0 — mark_orphan_signals.py
# [SPRINT 21-C COMPLETION: ORPHAN SIGNAL MARKING]
#
# BACKGROUND:
#   107 signals have result='FILLED' but outcome=NULL.
#   These are pending limit orders that filled and closed
#   between March 9–19 under the pre-Sprint 20 code.
#   Because save_trade() was never called for those fills,
#   no trade record exists in the DB to match against.
#   The backfill_signal_outcomes.py script correctly returned
#   0 matches because there is nothing to match to.
#
#   These signals must be excluded from ML training to
#   prevent them from corrupting the feature matrix.
#
# WHAT THIS DOES:
#   Sets result = 'ORPHANED_PRE_S20' on all FILLED signals
#   where outcome IS NULL. This value is not in the ml_pipeline
#   filter list ('FILLED', 'ATTEMPTED', 'EXECUTED'), so they
#   will be automatically excluded from all future training runs.
#
# SAFE TO RE-RUN: the WHERE clause is idempotent.
# ============================================================

import sqlite3

DB_PATH = "tradecore.db"


def mark_orphans():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    before = c.execute(
        "SELECT COUNT(*) FROM signals WHERE result = 'FILLED' AND outcome IS NULL"
    ).fetchone()[0]

    c.execute("""
        UPDATE signals
        SET result = 'ORPHANED_PRE_S20'
        WHERE result = 'FILLED' AND outcome IS NULL
    """)
    updated = c.rowcount
    conn.commit()

    after = c.execute(
        "SELECT COUNT(*) FROM signals WHERE result = 'FILLED' AND outcome IS NULL"
    ).fetchone()[0]
    orphan_total = c.execute(
        "SELECT COUNT(*) FROM signals WHERE result = 'ORPHANED_PRE_S20'"
    ).fetchone()[0]
    conn.close()

    print(f"--- Orphan Signal Marking ---")
    print(f"  Signals eligible (before) : {before}")
    print(f"  Marked ORPHANED_PRE_S20   : {updated}")
    print(f"  Remaining FILLED+no outcome: {after}  (should be 0)")
    print(f"  Total ORPHANED_PRE_S20    : {orphan_total}")
    if after == 0:
        print("✅ Complete. ML pipeline is now protected from orphan signals.")
    else:
        print("⚠️  Some signals could not be marked — check DB manually.")


if __name__ == "__main__":
    mark_orphans()

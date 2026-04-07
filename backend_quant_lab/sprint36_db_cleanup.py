# ============================================================
# Kom v1.0 — sprint36_db_cleanup.py
# [SPRINT 36: GOLD-ONLY DB RESET]
#
# PURPOSE:
#   Resets the signals table to Gold-only history.
#   All non-Gold signals (46,979 rows) are deleted.
#   All trades are preserved intact — both Gold and non-Gold.
#   Non-Gold trades are flagged with regime='ARCHIVED_S36' so
#   Kelly / ML pipelines filter them out automatically.
#
# WHAT THIS DOES:
#   1. Deletes all signals WHERE symbol NOT LIKE '%XAU%'
#   2. Tags non-Gold closed trades with account_id qualifier
#      so quant_analyzer account filtering excludes them
#   3. Vacuums the DB to reclaim space
#   4. Prints a before/after summary
#
# SAFE TO RE-RUN: All WHERE clauses are idempotent.
#
# HOW TO RUN (run ONCE before starting Sprint 36):
#   python sprint36_db_cleanup.py
# ============================================================

import sqlite3
from datetime import datetime

DB_PATH = "tradecore.db"

def cleanup():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    c = conn.cursor()

    print("=" * 54)
    print("  KOM v1.0 -- SPRINT 36 DB CLEANUP")
    print("=" * 54)

    # ── Before counts ─────────────────────────────────────────
    total_sigs  = c.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    gold_sigs   = c.execute("SELECT COUNT(*) FROM signals WHERE symbol LIKE '%XAU%'").fetchone()[0]
    total_trd   = c.execute("SELECT COUNT(*) FROM trades WHERE profit IS NOT NULL AND profit != 0 AND (comment IS NULL OR comment NOT LIKE '%ghost%')").fetchone()[0]
    gold_trd    = c.execute("SELECT COUNT(*) FROM trades WHERE symbol LIKE '%XAU%' AND profit IS NOT NULL AND profit != 0 AND (comment IS NULL OR comment NOT LIKE '%ghost%')").fetchone()[0]

    print(f"\n[BEFORE]")
    print(f"  Signals total    : {total_sigs:,}")
    print(f"  Gold signals     : {gold_sigs:,}")
    print(f"  Non-Gold signals : {total_sigs - gold_sigs:,}  ← will be deleted")
    print(f"  Closed trades    : {total_trd}")
    print(f"  Gold trades      : {gold_trd}")
    print(f"  Non-Gold trades  : {total_trd - gold_trd}  ← preserved, flagged ARCHIVED")

    # ── Step 1: Delete non-Gold signals ───────────────────────
    print(f"\n[1/3] Deleting non-Gold signals...")
    deleted = c.execute("""
        DELETE FROM signals WHERE symbol NOT LIKE '%XAU%'
    """).rowcount
    conn.commit()
    print(f"  Deleted: {deleted:,} signals")

    # ── Step 2: Flag non-Gold trades as ARCHIVED ──────────────
    # This prevents them from contaminating Kelly / ML while
    # preserving the history for manual review.
    print(f"\n[2/3] Archiving non-Gold trades...")
    archived = c.execute("""
        UPDATE trades
        SET regime = 'ARCHIVED_S36'
        WHERE symbol NOT LIKE '%XAU%'
          AND (comment IS NULL OR comment NOT LIKE '%ghost%')
          AND profit IS NOT NULL
    """).rowcount
    conn.commit()
    print(f"  Archived: {archived} non-Gold trade records")

    # ── Step 3: Vacuum DB ─────────────────────────────────────
    print(f"\n[3/3] Vacuuming database...")
    conn.execute("PRAGMA wal_checkpoint(FULL)")
    conn.close()
    # VACUUM must run outside a transaction
    conn2 = sqlite3.connect(DB_PATH)
    conn2.execute("VACUUM")
    conn2.close()
    print(f"  Vacuum complete")

    # ── After counts ──────────────────────────────────────────
    conn3 = sqlite3.connect(DB_PATH)
    after_sigs = conn3.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    after_gold = conn3.execute("SELECT COUNT(*) FROM signals WHERE symbol LIKE '%XAU%'").fetchone()[0]
    conn3.close()

    print(f"\n[AFTER]")
    print(f"  Signals total    : {after_sigs:,}")
    print(f"  Gold signals     : {after_gold:,}")
    print(f"  Non-Gold signals : {after_sigs - after_gold}")
    print(f"\n  [OK] DB is now Gold-only for live operations.")
    print("=" * 54)


if __name__ == "__main__":
    cleanup()

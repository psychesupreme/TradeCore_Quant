# ============================================================
# TradeCore v53.0 — sync_db.py  [SPRINT 9 REWRITE]
#
# SPRINT 9 CHANGES:
#   [S9-1] Route trade closes through DBManager.close_trade()
#          Previously used raw SQL UPDATE — return_pct never computed.
#   [S9-2] DB rotation: 90-day snapshots, 60-day skips, 30-day rejects.
#   [S9-3] deal.entry == DEAL_ENTRY_OUT filter prevents swap-row misread.
# ============================================================

import sqlite3
import MetaTrader5 as mt5
from datetime import datetime, timedelta
from db_manager import DBManager


def sync_database():
    """Called every 5 minutes. Closes ghost trades + rotates old DB rows."""
    if not mt5.initialize():
        return

    conn = sqlite3.connect('tradecore.db')
    cursor = conn.cursor()

    try:
        # ── 1. GHOST TRADE CLEANUP ─────────────────────────────
        df_open = cursor.execute(
            "SELECT ticket, symbol FROM trades WHERE close_time IS NULL"
        ).fetchall()

        closed_count   = 0
        total_realized = 0.0

        for ticket, symbol in df_open:
            ticket  = int(ticket)
            history = mt5.history_deals_get(ticket=ticket)
            if not history:
                continue

            # [S9-3] Find closing deal (DEAL_ENTRY_OUT=1); skip swap/entry rows
            close_deal = None
            for deal in reversed(history):
                if getattr(deal, 'entry', -1) == mt5.DEAL_ENTRY_OUT:
                    close_deal = deal
                    break
            if close_deal is None and history:
                close_deal = history[-1]   # fallback for brokers that omit entry flag
            if close_deal is None:
                continue

            close_time = datetime.utcfromtimestamp(close_deal.time).strftime('%Y-%m-%d %H:%M:%S')
            net_profit = (
                close_deal.profit
                + getattr(close_deal, 'swap',       0.0)
                + getattr(close_deal, 'commission', 0.0)
            )

            # [S9-1] Route through DBManager — computes return_pct correctly
            DBManager.close_trade(
                ticket      = ticket,
                close_price = close_deal.price,
                close_time  = close_time,
                profit      = net_profit,
                commission  = getattr(close_deal, 'commission', 0.0),
                slippage    = 0.0,
            )
            closed_count   += 1
            total_realized += net_profit

        conn.commit()
        if closed_count > 0:
            print(f"🧹 DB Sync: Closed {closed_count} ghost trades. "
                  f"Realized: ${total_realized:.2f}")

        # ── 2. DB ROTATION ──────────────────────────────────────
        _rotate_database(cursor)
        conn.commit()

    except Exception as e:
        print(f"⚠️ Sync Error: {e}")
    finally:
        conn.close()
        mt5.shutdown()


def _rotate_database(cursor):
    """
    [S9-2] Rolling cleanup — trades table is NEVER pruned.
    account_snapshots: 90 days  (Markov/VaR needs 200+ obs = ~3.5 hours)
    signals SKIPPED:   60 days  (not used for quant training)
    signals REJECTED:  30 days  (pattern known after a few days)
    """
    try:
        now         = datetime.utcnow()
        cut_snap    = (now - timedelta(days=90)).strftime('%Y-%m-%d %H:%M:%S')
        cut_skip    = (now - timedelta(days=60)).strftime('%Y-%m-%d %H:%M:%S')
        cut_rej     = (now - timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S')

        r1 = cursor.execute(
            "DELETE FROM account_snapshots WHERE timestamp < ?", (cut_snap,)
        ).rowcount
        r2 = cursor.execute(
            "DELETE FROM signals WHERE result = 'SKIPPED' AND timestamp < ?",
            (cut_skip,)
        ).rowcount
        r3 = cursor.execute(
            "DELETE FROM signals WHERE result LIKE 'REJECTED%' AND timestamp < ?",
            (cut_rej,)
        ).rowcount

        total = r1 + r2 + r3
        if total > 0:
            print(f"🗜️  DB Rotation: -{r1} snapshots, -{r2} skipped, -{r3} rejected.")
    except Exception as e:
        print(f"⚠️ DB Rotation Error: {e}")


if __name__ == "__main__":
    print("--- 🔄 RUNNING MANUAL DATABASE SYNC ---")
    sync_database()

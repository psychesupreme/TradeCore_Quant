# ============================================================
# TradeCore v53.0 — sync_db.py  [SPRINT 14 — BUG-44 FIXES]
#
# SPRINT 9 ADDITIONS (preserved):
#   [S9-1] Route trade closes through DBManager.close_trade()
#   [S9-2] DB rotation: 90-day snapshots, 60-day skips, 30-day rejects.
#   [S9-3] deal.entry == DEAL_ENTRY_OUT filter prevents swap-row misread.
#
# SPRINT 14 FIXES:
#   [BUG-44a] REMOVED mt5.shutdown() from finally block.
#     sync_database() ran every 5 min via scheduler while the bot had an
#     active MT5 connection. mt5.shutdown() closed the SHARED terminal
#     connection, making all subsequent mt5.* calls in bot_engine return
#     None for several seconds until the gateway auto-reconnect fired.
#     Fix: only shut down if WE initialised (standalone mode).
#
#   [BUG-44b] FIXED history_deals_get(ticket=...) → (position=...)
#     history_deals_get(ticket=X) looks up a DEAL by deal ticket number.
#     Our DB stores the POSITION/ORDER ticket from order_send result.order.
#     These are DIFFERENT numbers. Correct call: history_deals_get(position=X)
#     which returns all deals for that position ID (open + close deal).
#     This was why zero ghost trades ever got marked closed by sync_db.
#
#   [BUG-44c] close detection is now also in run_cycle() (every 60s).
#     sync_db is a 5-minute safety net for trades closed while bot was down.
# ============================================================

import sqlite3
import MetaTrader5 as mt5
from datetime import datetime, timedelta
from db_manager import DBManager


def sync_database():
    """
    Called every 5 minutes by the scheduler.
    1. Safety-net close detection for positions closed during bot downtime.
    2. DB rotation: prune old rows to keep the DB lean.

    [BUG-44a FIX] MT5 connection management:
    - If bot is running, MT5 is already connected — do NOT call shutdown().
    - If running standalone, we init and own the shutdown.
    """
    _we_own_connection = False
    if mt5.terminal_info() is None:
        if not mt5.initialize():
            return
        _we_own_connection = True

    conn = sqlite3.connect('tradecore.db')
    cursor = conn.cursor()

    try:
        # ── 1. SAFETY-NET CLOSE DETECTION ──────────────────────
        # Primary close detection runs every 60s in run_cycle().
        # This block catches trades closed while the bot was down,
        # or manual closes made directly in the MT5 terminal.
        df_open = cursor.execute(
            "SELECT ticket, symbol FROM trades WHERE close_time IS NULL AND profit IS NULL"
        ).fetchall()

        live_positions = mt5.positions_get() or []
        live_tickets   = {p.ticket for p in live_positions}

        closed_count   = 0
        total_realized = 0.0

        for ticket, symbol in df_open:
            ticket = int(ticket)

            if ticket in live_tickets:
                continue   # still open in MT5 — nothing to do

            # [BUG-44b FIX] position=ticket, NOT ticket=ticket
            history = mt5.history_deals_get(position=ticket)
            if not history:
                continue

            # [S9-3] Find closing deal (DEAL_ENTRY_OUT=1)
            close_deal = None
            for deal in reversed(history):
                if getattr(deal, 'entry', -1) == mt5.DEAL_ENTRY_OUT:
                    close_deal = deal
                    break
            if close_deal is None and history:
                close_deal = history[-1]
            if close_deal is None:
                continue

            close_time = datetime.utcfromtimestamp(
                close_deal.time
            ).strftime('%Y-%m-%d %H:%M:%S')
            net_profit = (
                close_deal.profit
                + getattr(close_deal, 'swap',       0.0)
                + getattr(close_deal, 'commission', 0.0)
            )

            # [S9-1] [BUG-51] close_trade has AND close_time IS NULL guard
            DBManager.close_trade(
                ticket      = ticket,
                close_price = close_deal.price,
                close_time  = close_time,
                profit      = net_profit,
                commission  = getattr(close_deal, 'commission', 0.0),
                slippage    = 0.0,
            )

            # [BUG-46] Update originating signal outcome
            outcome = "WIN" if net_profit > 0 else ("BREAK_EVEN" if net_profit == 0 else "LOSS")
            # [BUG-A FIX] MT5 deal objects have no 'price_open' attribute — the
            # fallback was close_deal.price making pips_result = 0.0 always.
            # Correct approach: fetch open_price from the trades table by ticket.
            try:
                open_px_row = conn.execute(
                    "SELECT open_price, type FROM trades WHERE ticket = ?", (ticket,)
                ).fetchone()
                if open_px_row and open_px_row[0]:
                    direction   = 1 if (open_px_row[1] or "BUY") == "BUY" else -1
                    pips_result = (close_deal.price - open_px_row[0]) * direction
                else:
                    pips_result = 0.0
            except Exception:
                pips_result = 0.0
            DBManager.update_signal_outcome(symbol, outcome, pips_result)

            closed_count   += 1
            total_realized += net_profit

        conn.commit()
        if closed_count > 0:
            print(f"🧹 DB Sync [safety-net]: Closed {closed_count} trade(s). "
                  f"Realized: ${total_realized:.2f}")

        # ── 2. DB ROTATION ──────────────────────────────────────
        _rotate_database(cursor)
        conn.commit()

    except Exception as e:
        print(f"⚠️ Sync Error: {e}")
    finally:
        conn.close()
        if _we_own_connection:       # [BUG-44a FIX] only if we opened it
            mt5.shutdown()


def _rotate_database(cursor):
    """
    [S9-2] Rolling cleanup — trades table is NEVER pruned.
    account_snapshots: 90 days
    signals SKIPPED:   60 days
    signals REJECTED:  30 days
    """
    try:
        now      = datetime.utcnow()
        cut_snap = (now - timedelta(days=90)).strftime('%Y-%m-%d %H:%M:%S')
        cut_skip = (now - timedelta(days=60)).strftime('%Y-%m-%d %H:%M:%S')
        cut_rej  = (now - timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S')

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

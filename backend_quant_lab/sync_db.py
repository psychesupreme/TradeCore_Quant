# ============================================================
# TradeCore v51.0 — sync_db.py
# SPRINT 2 FIXES APPLIED:
#   [BUG-08] Fixed wrong MT5 API: was history_deals_get(ticket=)
#            which looks up deal tickets. Should be position= to
#            look up deals belonging to a position. These are
#            different ID namespaces in MT5.
#   [QUALITY] Now uses DBManager.update_trade_close() instead
#             of raw SQL — all DB writes go through one place.
#   [QUALITY] Added signal outcome writing after each close
#             (WIN/LOSS/BE) to build the ML training dataset.
#   [QUALITY] Added clear logging of what was synced and why.
# ============================================================

import sqlite3
import pandas as pd
import MetaTrader5 as mt5
from datetime import datetime
from db_manager import DBManager


def _classify_outcome(profit: float, open_price: float, close_price: float,
                      trade_type: str, point: float = 0.0001) -> tuple[str, float]:
    """
    Returns (outcome, pips_result) for a closed trade.
    outcome: 'WIN' | 'LOSS' | 'BE'
    BE threshold: within 1 pip of entry.
    """
    if trade_type == 'BUY':
        pips = (close_price - open_price) / point
    else:
        pips = (open_price - close_price) / point

    if abs(pips) < 1.0:
        return 'BE', pips
    elif pips > 0:
        return 'WIN', pips
    else:
        return 'LOSS', pips


def sync_database():
    """
    Automated worker that reconciles SQLite with MT5 live state.
    Runs every 5 minutes via APScheduler.

    Logic:
    1. Get all trades the DB thinks are still open (close_time IS NULL).
    2. Get all position tickets that MT5 currently has open.
    3. Any DB trade NOT in MT5's open positions is closed — fetch the
       closing deal and update the record.
    4. Write signal outcome to signals table for ML training.
    """
    if not mt5.initialize():
        print("⚠️  sync_db: MT5 not available — skipping this sync cycle.")
        return

    conn = sqlite3.connect('tradecore.db')

    try:
        # Step 1: What does the DB think is open?
        df = pd.read_sql_query(
            "SELECT ticket, symbol, type, volume, open_price, open_time "
            "FROM trades WHERE close_time IS NULL",
            conn
        )

        if df.empty:
            return  # Nothing to sync

        # Step 2: What is actually open in MT5 right now?
        # [BUG-08 FIX] We compare position tickets, not deal tickets.
        live_positions = mt5.positions_get() or []
        alive_tickets  = {int(p.ticket) for p in live_positions}

        closed_count  = 0
        total_pnl     = 0.0

        for _, row in df.iterrows():
            ticket     = int(row['ticket'])
            symbol     = row['symbol']
            trade_type = row['type']
            open_price = float(row['open_price'])
            open_time  = row['open_time']

            if ticket in alive_tickets:
                continue  # Still open — nothing to do

            # ── POSITION IS CLOSED ──────────────────────────────
            # [BUG-08 FIX] Use position= keyword, NOT ticket=
            # history_deals_get(ticket=X) searches the DEAL table by deal ID.
            # history_deals_get(position=X) searches by the position ID — which
            # is what we stored. These are completely separate namespaces in MT5.
            history = mt5.history_deals_get(position=ticket)

            if not history:
                # Not found in history either — trade may be very recent.
                # Leave it open in DB; next cycle will catch it.
                continue

            # The closing deal is the last one in the position's history
            # (entry type 1 = out, type 2 = inout/reverse)
            closing_deal = None
            for deal in reversed(history):
                if deal.entry in (1, 2):
                    closing_deal = deal
                    break

            if closing_deal is None:
                # History exists but no exit deal yet — still opening
                continue

            close_price = closing_deal.price
            profit      = closing_deal.profit
            close_time  = datetime.fromtimestamp(closing_deal.time)
            hold_minutes = int((closing_deal.time - (closing_deal.time - 1)) / 60) if open_time else 0

            # Try to compute hold duration properly
            try:
                open_dt    = pd.to_datetime(open_time)
                hold_minutes = int((close_time - open_dt).total_seconds() / 60)
            except Exception:
                hold_minutes = 0

            # Write close data
            DBManager.update_trade_close(ticket, close_price, close_time, profit)

            # Write signal outcome for ML training
            outcome, pips = _classify_outcome(profit, open_price, close_price, trade_type)
            DBManager.update_signal_outcome(symbol, open_time, outcome, pips, hold_minutes)

            closed_count  += 1
            total_pnl     += profit
            print(f"🧹 Sync: Closed {symbol} #{ticket} | "
                  f"{outcome} {pips:+.1f}p | P&L: ${profit:+.2f}")

        if closed_count > 0:
            print(f"✅ DB Sync Complete: {closed_count} trade(s) closed. "
                  f"Session P&L: ${total_pnl:+.2f}")

    except Exception as e:
        print(f"⚠️  Sync Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        conn.close()


if __name__ == "__main__":
    print("--- 🔄 RUNNING MANUAL DATABASE SYNC ---")
    sync_database()
    print("--- SYNC COMPLETE ---")

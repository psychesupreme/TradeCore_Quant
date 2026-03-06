-- =============================================================
-- TradeCore v51.0 — Ghost Trade Cleanup
-- =============================================================
-- BACKGROUND:
--   139 trades have NULL close_time dating back to 2026-02-18.
--   These are "ghost" positions — the broker account was reset
--   or MT5 was restarted between Feb 18 and Mar 1, leaving these
--   trades open in the DB with no matching live position.
--
--   sync_db.py was using history_deals_get(ticket=) instead of
--   history_deals_get(position=) — so it could never match and
--   close them. That bug is now fixed in Sprint 5.
--
--   However these 139 rows still need to be cleaned up manually
--   because sync_db will now only close trades that MT5's history
--   confirms as closed. Positions from a reset/disconnected account
--   will not appear in MT5 history and will stay ghost forever.
--
-- WHAT THIS DOES:
--   Sets close_time = open_time, close_price = open_price,
--   profit = 0.0, and comment = 'ghost_cleanup' for all trades
--   that pre-date March 1 with no close data.
--
--   This removes them from sync_db's open-trade scan and from
--   the MAX_OPEN_TRADES count without falsifying P&L.
--
-- HOW TO RUN:
--   From the project root:
--     sqlite3 tradecore.db < ghost_cleanup.sql
--   OR paste directly into DB Browser for SQLite.
--
-- SAFE TO RE-RUN: The WHERE clause prevents touching real trades.
-- =============================================================

-- Preview first (comment out the UPDATE to do a dry run)
SELECT COUNT(*) AS ghost_count
FROM trades
WHERE close_time IS NULL
  AND open_time < '2026-03-01 00:00:00';

-- Execute cleanup
UPDATE trades
SET
    close_time  = open_time,
    close_price = open_price,
    profit      = 0.0,
    comment     = 'ghost_cleanup'
WHERE
    close_time IS NULL
    AND open_time < '2026-03-01 00:00:00';

-- Confirm result
SELECT
    COUNT(*)                                     AS total_trades,
    SUM(CASE WHEN close_time IS NULL THEN 1 ELSE 0 END) AS still_open,
    SUM(CASE WHEN comment = 'ghost_cleanup' THEN 1 ELSE 0 END) AS cleaned
FROM trades;

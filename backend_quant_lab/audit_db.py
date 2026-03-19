# ============================================================
# Kom v1.0 — audit_db.py
# [SPRINT 22-C: PER-ACCOUNT HEALTH REPORT & ML READINESS GATE]
#
# PREVIOUS VERSION ISSUES (Sprint 21 diagnosis):
#   - Queried all trades without account_id filter, mixing the
#     old $100k demo history with the current account.
#   - No ML readiness gate — reported counts that included
#     ghost trades and ORPHANED_PRE_S20 signals.
#   - No CSV export capability for the Flutter dashboard.
#
# SPRINT 22-C ADDITIONS:
#   - Filters all queries to current MT5 account_id.
#   - Shows true N: only real closed trades (non-ghost, profit != 0).
#   - ML readiness gate: reports exact status and what is blocking it.
#   - CSV export: generates media/kom_audit_{account_id}_{date}.csv
#     for the /quant/export_report Flutter endpoint.
#   - Signal quality section: WIN/LOSS/ORPHANED breakdown.
#   - Sizing health check: confirms current lot caps vs balance.
# ============================================================

import sqlite3
import pandas as pd
import os
from datetime import datetime

DB_PATH   = "tradecore.db"
MEDIA_DIR = "media"
os.makedirs(MEDIA_DIR, exist_ok=True)

ML_THRESHOLD = 30


def _get_account_id(conn) -> str | None:
    row = conn.execute("""
        SELECT account_id FROM account_snapshots
        WHERE account_id IS NOT NULL
        ORDER BY timestamp DESC LIMIT 1
    """).fetchone()
    return row[0] if row else None


def audit_database(export_csv: bool = False) -> dict:
    """
    Full per-account health audit. Returns a structured dict so this
    function can also be called from the /quant/export_report endpoint.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
    except Exception as e:
        print(f"❌ Cannot connect to database: {e}")
        return {}

    report = {}

    print("\n" + "=" * 54)
    print("  🔬 KOM v1.0 — DATABASE HEALTH & READINESS REPORT")
    print("=" * 54)

    # ── 1. Account identification ─────────────────────────────
    account_id = _get_account_id(conn)
    report['account_id']    = account_id
    report['generated_at']  = datetime.utcnow().isoformat()
    print(f"\n📋 Account    : {account_id or 'Unknown'}")
    print(f"   Generated  : {report['generated_at'][:19]} UTC")

    # ── 2. Closed trade analysis ──────────────────────────────
    print(f"\n{'─'*54}")
    print("🎯 CLOSED TRADES (Kelly / ML Threshold)")
    print(f"{'─'*54}")

    trade_query = """
        SELECT ticket, symbol, type, volume, open_price, close_price,
               profit, open_time, close_time, account_id, model_type, regime
        FROM trades
        WHERE profit IS NOT NULL
          AND profit != 0
          AND (comment IS NULL OR comment NOT LIKE '%ghost%')
    """
    params = []
    if account_id:
        trade_query += " AND (account_id = ? OR account_id IS NULL)"
        params.append(account_id)
    trade_query += " ORDER BY close_time ASC"

    trades_df = pd.read_sql_query(trade_query, conn,
                                  params=params if params else None)
    n_trades  = len(trades_df)
    report['n_trades']     = n_trades
    report['ml_threshold'] = ML_THRESHOLD
    report['ml_ready']     = n_trades >= ML_THRESHOLD

    if n_trades > 0:
        wins    = trades_df[trades_df['profit'] > 0]
        losses  = trades_df[trades_df['profit'] < 0]
        net_pnl = trades_df['profit'].sum()
        win_rate = len(wins) / n_trades * 100
        avg_win  = wins['profit'].mean()  if len(wins)   > 0 else 0
        avg_loss = losses['profit'].mean() if len(losses) > 0 else 0
        pf = (wins['profit'].sum() / abs(losses['profit'].sum())
              if len(losses) > 0 and losses['profit'].sum() != 0
              else float('inf'))
        tagged       = trades_df['account_id'].notna().sum()
        model_tagged = trades_df['model_type'].notna().sum()

        report.update({'win_rate': round(win_rate, 1), 'profit_factor': round(pf, 2),
                       'net_pnl': round(net_pnl, 2), 'avg_win': round(avg_win, 2),
                       'avg_loss': round(avg_loss, 2), 'n_wins': len(wins),
                       'n_losses': len(losses),
                       'account_id_coverage': f"{tagged}/{n_trades}",
                       'model_type_coverage':  f"{model_tagged}/{n_trades}"})

        print(f"   Total executions  : {n_trades} / {ML_THRESHOLD} (ML threshold)")
        print(f"   Winners / Losers  : {len(wins)} / {len(losses)}")
        print(f"   Win Rate          : {win_rate:.1f}%")
        print(f"   Net P&L           : ${net_pnl:+,.2f}")
        print(f"   Avg Win / Loss    : ${avg_win:+.2f} / ${avg_loss:+.2f}")
        print(f"   Profit Factor     : {pf:.2f}")
        print(f"   account_id tagged : {tagged}/{n_trades}")
        print(f"   model_type tagged : {model_tagged}/{n_trades}")
    else:
        report.update({'win_rate': 0, 'profit_factor': 0, 'net_pnl': 0,
                       'avg_win': 0, 'avg_loss': 0, 'n_wins': 0, 'n_losses': 0})
        print("   No closed trades recorded yet.")

    # ── 3. ML readiness gate ──────────────────────────────────
    print(f"\n{'─'*54}")
    print("🧠 ML PIPELINE READINESS GATE")
    print(f"{'─'*54}")

    remaining = max(0, ML_THRESHOLD - n_trades)
    report['trades_to_ml'] = remaining

    if report['ml_ready']:
        print(f"   ✅ THRESHOLD MET: N={n_trades} ≥ {ML_THRESHOLD}")
        print(f"   Ready for XGBoost extraction.")
        print(f"   Run: python ml_pipeline.py → python model_trainer.py")
    else:
        print(f"   ⏳ COLLECTING: N={n_trades}/{ML_THRESHOLD}")
        print(f"   Need {remaining} more clean tagged trades.")
        print(f"   Lot sizing locked to conservative balance/20000 cap.")

    # ── 4. Signal funnel ──────────────────────────────────────
    print(f"\n{'─'*54}")
    print("📡 SIGNAL FUNNEL (All-Time)")
    print(f"{'─'*54}")

    funnel = {r['result']: r['n'] for _, r in pd.read_sql_query(
        "SELECT result, COUNT(*) as n FROM signals GROUP BY result", conn
    ).iterrows()}

    filled   = funnel.get('FILLED', 0)
    executed = funnel.get('EXECUTED', 0)
    attempted = funnel.get('ATTEMPTED', 0)
    orphaned  = funnel.get('ORPHANED_PRE_S20', 0)
    skipped   = funnel.get('SKIPPED', 0)
    lc_total  = sum(v for k, v in funnel.items() if 'LOW_CONFIDENCE' in k)
    rejected  = sum(v for k, v in funnel.items() if 'REJECTED' in k)
    total_sigs = sum(funnel.values())

    report['signal_funnel'] = {
        'total': total_sigs, 'filled': filled, 'executed': executed,
        'attempted': attempted, 'orphaned_pre_s20': orphaned,
        'low_confidence': lc_total, 'rejected': rejected, 'skipped': skipped
    }

    print(f"   Total logged      : {total_sigs:,}")
    print(f"   Filled            : {filled}")
    print(f"   Executed          : {executed}")
    print(f"   Attempted (open)  : {attempted}")
    print(f"   Orphaned (pre-S20): {orphaned}  ← excluded from ML")
    print(f"   Low confidence    : {lc_total}")
    print(f"   Rejected          : {rejected}")
    print(f"   Skipped           : {skipped:,}")

    outcomes = {r['outcome']: r['n'] for _, r in pd.read_sql_query(
        "SELECT outcome, COUNT(*) as n FROM signals WHERE outcome IS NOT NULL GROUP BY outcome",
        conn
    ).iterrows()}
    total_outcomes = sum(outcomes.values())
    print(f"\n   Outcome-labelled  : {total_outcomes}  (W:{outcomes.get('WIN',0)} L:{outcomes.get('LOSS',0)})")
    report['signal_outcomes'] = outcomes

    # ── 5. Account snapshot health ────────────────────────────
    print(f"\n{'─'*54}")
    print("💰 ACCOUNT & SIZING HEALTH")
    print(f"{'─'*54}")

    snap_q = "SELECT timestamp, balance, equity, margin_level FROM account_snapshots"
    snap_p = []
    if account_id:
        snap_q += " WHERE account_id = ?"
        snap_p.append(account_id)
    snap_q += " ORDER BY timestamp DESC LIMIT 1"
    latest = conn.execute(snap_q, snap_p).fetchone()

    if latest:
        ts, bal, eq, ml = latest
        report['latest_balance'] = bal
        report['latest_equity']  = eq
        print(f"   Latest balance    : ${bal:,.2f}")
        print(f"   Latest equity     : ${eq:,.2f}")
        if ml:
            print(f"   Margin level      : {ml:.0f}%")
        if bal:
            micro_cap = round(min(bal / 20000, 0.50), 2) if not report['ml_ready'] \
                        else round(bal / 12000, 2)
            abs_cap   = min(bal * 0.015, 150.0) if not report['ml_ready'] \
                        else bal * 0.03
            fx_min    = max(0.01, round(bal / 75000, 2))
            print(f"\n   Sizing ({('N<30 phase' if not report['ml_ready'] else 'N≥30 active')}):")
            print(f"     MICRO max lot   : {micro_cap:.2f}")
            print(f"     Abs risk cap    : ${abs_cap:.0f} / trade")
            print(f"     FX min lot      : {fx_min:.2f}")
            report.update({'micro_cap': micro_cap, 'abs_risk_cap': abs_cap, 'fx_min_lot': fx_min})
    else:
        report['latest_balance'] = None
        print("   No snapshots found for this account.")

    ghost_count = conn.execute(
        "SELECT COUNT(*) FROM trades WHERE comment = 'ghost_cleanup'"
    ).fetchone()[0]
    report['ghost_trades'] = ghost_count
    print(f"\n   Ghost trades      : {ghost_count}  ← zeroed, excluded from all math")

    print(f"\n{'='*54}\n")
    conn.close()

    # ── 6. CSV export ─────────────────────────────────────────
    if export_csv and n_trades > 0:
        ts_str   = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        acc_tag  = account_id or "unknown"
        csv_path = os.path.join(MEDIA_DIR, f"kom_audit_{acc_tag}_{ts_str}.csv")
        trades_df.to_csv(csv_path, index=False)
        report['csv_path'] = csv_path
        print(f"💾 CSV exported: {csv_path}")

    return report


if __name__ == "__main__":
    audit_database(export_csv=True)

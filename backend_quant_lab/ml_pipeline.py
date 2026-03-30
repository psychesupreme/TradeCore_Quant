# ============================================================
# Kom v1.0 — ml_pipeline.py
# [SPRINT 23-A: ML PIPELINE ACTIVATION]
#
# SPRINT 23 FIXES (over Sprint 18b/c original):
#   [S23-A-1] NULL-safe ghost filter: replaced
#             comment NOT LIKE '%ghost%'
#             with (comment IS NULL OR comment NOT LIKE '%ghost%')
#             — the original filter silently excluded 34 trades.
#
#   [S23-A-2] Timestamp anomaly guard: filters out trades where
#             close_time < open_time (USDCHF #8578679486 BUG-55
#             residual). Negative hold durations corrupt hold_min.
#
#   [S23-A-3] Signal feature join: attempts to enrich each trade
#             with ict_score, kill_zone, and confidence from the
#             signals table (±30-minute window match). Falls back
#             to NULL for trades with no matching signal.
#
#   [S23-A-4] Volume dropped: raw lots are not comparable across
#             assets (XAGUSD 0.72 ≠ EURUSD 0.72 in dollar risk).
#             Replaced with dollar_risk = sl_distance × lot_value.
#
#   [S23-A-5] Stratified k-fold cross-validation added to
#             model_trainer.py — at N=41 an 80/20 holdout gives
#             only ~8 test samples, making accuracy metrics noise.
#
#   [S23-A-6] Account-ID filtering: only extracts trades from the
#             current account (account_id = '32128474' or NULL for
#             pre-S20 trades) to prevent cross-account contamination.
#
# PRESERVED FROM SPRINT 18:
#   - Cyclical hour encoding (sin/cos)
#   - One-hot encoding for asset class and regime
#   - N=30 hard lock gate
#   - timezone-aware UTC timestamp (BUG-69 fix)
# ============================================================

import sqlite3
import pandas as pd
import numpy as np
from datetime import datetime, timezone
import os


class MLDataExtractor:
    def __init__(self, db_path="tradecore.db"):
        self.db_path    = db_path
        self.export_dir = "media"
        os.makedirs(self.export_dir, exist_ok=True)

    def _get_account_id(self, conn) -> str | None:
        row = conn.execute("""
            SELECT account_id FROM account_snapshots
            WHERE account_id IS NOT NULL
            ORDER BY timestamp DESC LIMIT 1
        """).fetchone()
        return row[0] if row else None

    def extract_trade_data(self) -> pd.DataFrame:
        """
        Pulls all closed trades that are NOT ghost trades.
        [S23-A-1] NULL-safe ghost filter.
        [S23-A-2] Excludes timestamp anomalies (close < open).
        [S23-A-3] Left-joins signal features (ict_score, kill_zone, confidence).
        [S23-A-6] Filters to current account_id (or NULL for pre-S20 trades).
        """
        try:
            conn = sqlite3.connect(self.db_path)
            account_id = self._get_account_id(conn)

            # Base trade query — account-filtered, NULL-safe ghost exclusion
            trade_q = """
                SELECT
                    t.ticket, t.symbol, t.type, t.volume,
                    t.open_price, t.close_price, t.sl, t.tp,
                    t.profit, t.open_time, t.close_time, t.regime
                FROM trades t
                WHERE t.profit IS NOT NULL
                  AND t.profit != 0
                  AND (t.comment IS NULL OR t.comment NOT LIKE '%ghost%')
            """
            if account_id:
                trade_q += " AND (t.account_id = ? OR t.account_id IS NULL)"
                df = pd.read_sql_query(trade_q, conn, params=(account_id,))
            else:
                df = pd.read_sql_query(trade_q, conn)

            if df.empty:
                conn.close()
                return df

            # [S34] Filter out crypto trades — BTC/ETH removed from asset universe.
            # Including them skews the model with patterns from a discontinued strategy.
            before_crypto = len(df)
            df = df[~df['symbol'].str.contains('BTC|ETH', na=False)].copy()
            removed_crypto = before_crypto - len(df)
            if removed_crypto > 0:
                print(f"  [INFO] Filtered {removed_crypto} retired crypto trade(s) from training set.")

            # [S23-A-2] Remove timestamp anomalies
            df['open_time']  = pd.to_datetime(df['open_time'],  format='mixed')
            df['close_time'] = pd.to_datetime(df['close_time'], format='mixed')
            before = len(df)
            df = df[df['close_time'] > df['open_time']].copy()
            removed = before - len(df)
            if removed > 0:
                print(f"  [WARN] Removed {removed} trade(s) with invalid timestamps.")

            # [S23-A-3] Signal feature enrichment via left join
            sig_q = """
                SELECT symbol, timestamp, ict_score, kill_zone, confidence
                FROM signals
                WHERE result IN ('FILLED', 'EXECUTED', 'ORPHANED_PRE_S20')
                  AND ict_score IS NOT NULL
            """
            sigs = pd.read_sql_query(sig_q, conn)
            conn.close()

            if not sigs.empty:
                sigs['timestamp'] = pd.to_datetime(sigs['timestamp'], format='mixed')
                df = self._join_signal_features(df, sigs)
            else:
                df['ict_score']   = np.nan
                df['kill_zone']   = np.nan
                df['signal_conf'] = np.nan

            return df

        except Exception as e:
            print(f"❌ DB Extraction Error: {e}")
            return pd.DataFrame()

    def _join_signal_features(self, trades: pd.DataFrame,
                              signals: pd.DataFrame) -> pd.DataFrame:
        """
        For each trade, find the best-matching signal within ±30 minutes
        of the open_time for the same symbol. 'Best' = closest timestamp.
        """
        ict_scores, kill_zones, sig_confs = [], [], []

        for _, trade in trades.iterrows():
            sym  = trade['symbol']
            t_op = trade['open_time']
            window_start = t_op - pd.Timedelta(minutes=30)
            window_end   = t_op + pd.Timedelta(minutes=30)

            candidates = signals[
                (signals['symbol'] == sym) &
                (signals['timestamp'] >= window_start) &
                (signals['timestamp'] <= window_end)
            ]

            if candidates.empty:
                ict_scores.append(np.nan)
                kill_zones.append(None)
                sig_confs.append(np.nan)
            else:
                best = candidates.iloc[
                    (candidates['timestamp'] - t_op).abs().argsort().iloc[0]
                ]
                ict_scores.append(best['ict_score'])
                kill_zones.append(best['kill_zone'])
                sig_confs.append(best['confidence'])

        trades = trades.copy()
        trades['ict_score']   = ict_scores
        trades['kill_zone']   = kill_zones
        trades['signal_conf'] = sig_confs
        return trades

    def engineer_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Transforms raw trade data into an ML-ready numerical matrix.
        """
        if df.empty:
            print("[WARN] No data to vectorize.")
            return df

        print(f"[INFO] Engineering features for {len(df)} trades...")
        df = df.copy()

        # ── TARGET ────────────────────────────────────────────
        # 1 = Win, 0 = Loss. Breakevens excluded at extraction.
        df['target_win'] = (df['profit'] > 0).astype(int)

        # ── TRADE FEATURES ────────────────────────────────────
        # Direction
        df['is_buy'] = (df['type'] == 'BUY').astype(int)

        # Hold duration (minutes)
        df['hold_min'] = (df['close_time'] - df['open_time']).dt.total_seconds() / 60

        # SL distance (price units)
        df['sl_distance'] = (df['open_price'] - df['sl']).abs()
        df['sl_distance'] = df['sl_distance'].replace(0, np.nan)

        # TP distance and R:R ratio
        df['tp_distance'] = (df['tp'] - df['open_price']).abs()
        df['rr_ratio'] = (df['tp_distance'] / df['sl_distance']).replace([np.inf, -np.inf], np.nan)

        # [S23-A-4] Dollar risk (normalized across assets)
        # Approximate pip value per asset class — good enough for rank-ordering
        def pip_value(row):
            sym = str(row['symbol'])
            sl  = row['sl_distance']
            vol = row['volume']
            if pd.isna(sl) or sl == 0 or pd.isna(vol):
                return np.nan
            if 'XAU' in sym or 'XAG' in sym:
                if 'XAG' in sym:
                    return sl * 5000 * vol      # [S26] silver: 5000 oz/lot
                return sl * 100 * vol           # gold/oil: $100/lot/point
            if 'BTC' in sym or 'ETH' in sym:
                return sl * vol                 # crypto: $1/lot/point
            if 'JPY' in sym:
                return sl * 1000 * vol          # JPY pairs
            if any(x in sym for x in ['SP 500', 'Tech 100', 'Germany 40']):
                return sl * 10 * vol            # indices: approx $10/lot/point
            if 'Oil' in sym or 'NGAS' in sym:
                return sl * 100 * vol
            return sl * 100000 * vol            # standard FX: $100k/lot

        df['dollar_risk'] = df.apply(pip_value, axis=1)

        # ── SIGNAL CONFIDENCE ─────────────────────────────────
        # [S34] signal_conf is the execution confidence from the hybrid system.
        # ict_score removed — it encoded ICT_STANDARD internals which are
        # no longer the primary signal source.
        df['signal_conf'] = pd.to_numeric(df['signal_conf'], errors='coerce')
        df['signal_conf'] = df['signal_conf'].fillna(df['signal_conf'].median())

        # ── [S28] SMC STRUCTURAL FEATURES ────────────────────────
        # Extracted from ict_conditions JSON column when available.
        # These encode multi-timeframe FVG/OB/MSB confluence quality.
        # [S34] Hybrid-system-only feature extraction.
        # ICT/SMC internal scores removed (smc_*, amd_penalty, ict_score):
        # those features encoded the failing ICT_STANDARD model's internals.
        # Retained: M1 scalp system, session context, Conqueror 3-MA,
        # channel breakout, Silver AR, EURUSD modules, model-winner one-hots.
        def _extract_hybrid(row):
            cond = row.get('ict_conditions', '{}') or '{}'
            try:
                d = __import__('json').loads(cond) if isinstance(cond, str) else {}
            except Exception:
                d = {}
            mw = str(d.get('model_winner', ''))
            return pd.Series({
                # ── M1 scalp system ─────────────────────────────────
                'm1_scalp':          int(bool(d.get('m1_scalp', False))),
                'm1_range_aligned':  int(bool(d.get('range_aligned', False))),
                'm1_has_pullback':   int(bool(d.get('m1_has_pullback', False))),
                'h4_aligned':        int(bool(d.get('h4_aligned', True))),
                # ── Hybrid confluences (S30/S32) ─────────────────────
                'conqueror_bull':    int(bool(d.get('conqueror_bull', False))),
                'conqueror_bear':    int(bool(d.get('conqueror_bear', False))),
                'conqueror_bonus':   float(d.get('conqueror_bonus', 0.0)),
                'channel_breakout':  int(bool(d.get('channel_breakout_zone', False))),
                'channel_bonus':     float(d.get('channel_bonus', 0.0)),
                'ema50_bonus':       float(d.get('ema50_bonus', 0.0)),
                # ── Module one-hots (which strategy fired) ───────────
                'model_m1_scalp':    int(mw == 'M1_SCALP'),
                'model_silver_ar':   int(mw.startswith('SILVER_ASIAN')),
                'model_turtle_soup': int(mw.startswith('TURTLE_SOUP')),
                'model_fx_london':   int(mw.startswith('FX_LONDON')),
                'model_slingshot':   int(mw.startswith('SLINGSHOT')),
                'model_ict':         int(mw == '' or mw == 'ICT_STANDARD'),
                # ── EURUSD dedicated strategy (S33) ──────────────────
                'eu_london_breakout':  int(mw == 'EURUSD_LONDON_BREAKOUT'),
                'eu_ny_reversion':     int(mw == 'EURUSD_NY_REVERSION'),
                'eu_tight_range':      int(bool(d.get('eu_tight_range', False))),
                'eu_z_score':          float(d.get('eu_z_score', 0.0)),
                'eu_asian_range_pips': float(d.get('eu_asian_range_pips', 0.0)),
            })

        # Only extract if ict_conditions column exists
        if 'ict_conditions' in df.columns:
            hybrid_feats = df.apply(_extract_hybrid, axis=1)
            df = pd.concat([df, hybrid_feats], axis=1)
        else:
            # Add zero columns so feature alignment is stable
            for col in [
                'm1_scalp','m1_range_aligned','m1_has_pullback','h4_aligned',
                'conqueror_bull','conqueror_bear','conqueror_bonus',
                'channel_breakout','channel_bonus','ema50_bonus',
                'model_m1_scalp','model_silver_ar','model_turtle_soup',
                'model_fx_london','model_slingshot','model_ict',
                'eu_london_breakout','eu_ny_reversion','eu_tight_range',
                'eu_z_score','eu_asian_range_pips',
            ]:
                df[col] = 0

        # Kill zone one-hot encoding
        kz_known = ['Asian', 'London', 'London_NY', 'London_PM', 'NY_Open',
                    'NY_PM2', 'Other']
        df['kill_zone_clean'] = df['kill_zone'].fillna('Other')
        for kz in kz_known:
            df[f'kz_{kz}'] = (df['kill_zone_clean'] == kz).astype(int)

        # ── ASSET CLASS ───────────────────────────────────────
        def categorize_asset(sym):
            if any(x in sym for x in ['XAU', 'XAG', 'Oil', 'NGAS']): return 'Commodity'
            if any(x in sym for x in ['BTC', 'ETH']):                  return 'Crypto'
            if any(x in sym for x in ['SP 500', 'Tech 100', 'Germany']): return 'Index'
            return 'Forex'

        df['asset_class'] = df['symbol'].apply(categorize_asset)
        df = pd.get_dummies(df, columns=['asset_class'], prefix='asset')

        # ── REGIME ────────────────────────────────────────────
        df['clean_regime'] = df['regime'].str.split(' ').str[0].fillna('UNKNOWN')
        df = pd.get_dummies(df, columns=['clean_regime'], prefix='regime')

        # ── TEMPORAL ──────────────────────────────────────────
        df['execution_hour'] = df['open_time'].dt.hour
        # Cyclical encoding (23:00 is adjacent to 00:00)
        df['hour_sin'] = np.sin(2 * np.pi * df['execution_hour'] / 24)
        df['hour_cos'] = np.cos(2 * np.pi * df['execution_hour'] / 24)

        # Day of week (0=Mon … 4=Fri) — cyclical
        df['dow'] = df['open_time'].dt.dayofweek
        df['dow_sin'] = np.sin(2 * np.pi * df['dow'] / 5)
        df['dow_cos'] = np.cos(2 * np.pi * df['dow'] / 5)

        # ── DROP RAW / NON-NUMERIC COLUMNS ────────────────────
        drop_cols = [
            'type', 'regime', 'open_time', 'close_time',
            'open_price', 'close_price', 'sl', 'tp',
            'execution_hour', 'dow', 'kill_zone', 'kill_zone_clean',
            'volume',           # [S23-A-4] replaced by dollar_risk
            'ict_conditions',   # [S34] already extracted into hybrid feature cols
            'ict_score',        # [S34] ICT internal score removed; use signal_conf
        ]
        df_ml = df.drop(columns=[c for c in drop_cols if c in df.columns])

        # Ensure no NaN in numeric columns (XGBoost handles NaN natively but
        # imputing keeps the feature counts stable for the live scorer)
        num_cols = df_ml.select_dtypes(include=[np.number]).columns.tolist()
        for col in num_cols:
            if df_ml[col].isna().any():
                df_ml[col] = df_ml[col].fillna(df_ml[col].median())

        return df_ml

    def build_dataset(self) -> pd.DataFrame | None:
        print("\n" + "=" * 54)
        print("  KOM v1.0 -- ML PIPELINE EXTRACTION (S23-A)")
        print("=" * 54)

        raw_df = self.extract_trade_data()
        if raw_df.empty:
            print("[ERROR] Extraction halted: database empty or locked.")
            return None

        print(f"  Extracted: {len(raw_df)} trades")
        signal_matched = raw_df['ict_score'].notna().sum() \
            if 'ict_score' in raw_df.columns else 0
        print(f"  Signal features matched: {signal_matched}/{len(raw_df)}")

        ml_df = self.engineer_features(raw_df)

        n = len(ml_df)
        if n < 30:
            print(f"\n[WARN] N={n} -- below threshold (30). Export only, no training.")
        else:
            print(f"\n[OK] N={n} -- threshold met. Dataset ready for XGBoost.")

        timestamp   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        export_path = os.path.join(self.export_dir, f"training_matrix_{timestamp}.csv")
        ml_df.to_csv(export_path, index=False)
        print(f"[SAVE] Matrix saved: {export_path}")
        print(f"       Features: {[c for c in ml_df.columns if c not in ['ticket','symbol','profit','target_win']]}")
        print("=" * 54 + "\n")

        return ml_df


if __name__ == "__main__":
    extractor = MLDataExtractor()
    extractor.build_dataset()

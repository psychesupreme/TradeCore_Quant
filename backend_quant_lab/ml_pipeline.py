# ============================================================
# Kom v1.0 — ml_pipeline.py
# [PHASE C: MACHINE LEARNING EXTRACTION & VECTORIZATION]
#
# PURPOSE:
#   Safely extracts trade and signal data from the live database
#   without interrupting the Execution or Analysis loops. 
#   Transforms raw categorical heuristics into numerical matrices 
#   (Feature Engineering) for XGBoost/LightGBM ingestion.
#
# HISTORICAL PRESERVATION & ARCHITECTURE:
#   - [Sprint 18b] Initial pipeline draft. Connects to tradecore.db 
#     via read-only SQL queries to prevent GIL locking the main bot.
#   - [Sprint 18c] File routing updated to output directly to the 
#     centralized 'media/' directory for easier dashboard integration.
#   - [BUG-69 FIX] Patched Python 3.12+ `datetime.utcnow()` deprecation 
#     warning by utilizing `datetime.now(timezone.utc)`.
#
# FEATURE ENGINEERING LOGIC:
#   - Target Variable (Y): 1 = Profitable Trade, 0 = Losing Trade.
#     (Breakevens are pre-filtered out by the SQL query as they 
#      mathematically distort the Kelly Criterion 'b' ratio).
#   - Time Vectorization: Hours are converted into sine/cosine waves 
#     (Cyclical Encoding). This teaches the ML model that 23:00 is 
#     temporally right next to 00:00, rather than mathematically distant.
#   - Minimum Threshold: Hard lock at N=30 (Quarter-Kelly threshold) 
#     to prevent the XGBoost model from severely curve-fitting to noise.
# ============================================================

import sqlite3
import pandas as pd
import numpy as np
from datetime import datetime, timezone
import os

class MLDataExtractor:
    def __init__(self, db_path="tradecore.db"):
        self.db_path = db_path
        # [Sprint 18c] Centralized asset routing
        self.export_dir = "media"
        os.makedirs(self.export_dir, exist_ok=True)

    def extract_trade_data(self):
        """
        Pulls all closed trades that are NOT ghost trades.
        We only want real market variance for training.
        """
        try:
            con = sqlite3.connect(self.db_path)
            # [Sprint 18] Strict filtering: no ghosts, no zero-profit scratches
            query = """
                SELECT 
                    ticket, symbol, type, volume, open_price, close_price, 
                    sl, tp, profit, open_time, close_time, regime
                FROM trades 
                WHERE profit IS NOT NULL 
                AND profit != 0 
                AND comment NOT LIKE '%ghost%'
            """
            df_trades = pd.read_sql_query(query, con)
            con.close()
            return df_trades
        except Exception as e:
            print(f"❌ DB Extraction Error: {e}")
            return pd.DataFrame()

    def engineer_features(self, df):
        """
        Transforms raw trade data into ML-ready numerical matrices.
        """
        if df.empty:
            print("⚠️ No data available to vectorize.")
            return df

        print(f"⚙️ Engineering features for {len(df)} live executions...")

        # 1. TARGET VARIABLE (Y): 1 if Win, 0 if Loss
        df['target_win'] = (df['profit'] > 0).astype(int)

        # 2. FEATURE: Trade Direction (1 for BUY, 0 for SELL)
        df['is_buy'] = (df['type'] == 'BUY').astype(int)

        # 3. FEATURE: Risk/Reward Profile
        # Note: Avoid division by zero if SL was not set natively
        df['sl_distance'] = abs(df['open_price'] - df['sl'])
        df['sl_distance'] = df['sl_distance'].replace(0, np.nan) # Prevent inf corruption
        
        # 4. FEATURE: Asset Class Categorization
        def categorize_asset(symbol):
            if any(x in symbol for x in ['XAU', 'XAG', 'Oil', 'NGAS']): return 'Commodity'
            if any(x in symbol for x in ['BTC', 'ETH']): return 'Crypto'
            if any(x in symbol for x in ['SP 500', 'Tech 100', 'Germany']): return 'Index'
            return 'Forex'
            
        df['asset_class'] = df['symbol'].apply(categorize_asset)
        
        # One-Hot Encode Asset Classes (e.g., asset_Commodity: 1 or 0)
        df = pd.get_dummies(df, columns=['asset_class'], prefix='asset')

        # 5. FEATURE: Market Regime Vectorization
        # Clean the regime strings (e.g. "NORMAL (TRENDING)" -> "NORMAL")
        df['clean_regime'] = df['regime'].str.split(' ').str[0].fillna('UNKNOWN')
        df = pd.get_dummies(df, columns=['clean_regime'], prefix='regime')

        # 6. FEATURE: Temporal Analysis (Hour of Execution)
        df['open_time'] = pd.to_datetime(df['open_time'])
        df['execution_hour'] = df['open_time'].dt.hour
        
        # [PHASE C CORE] Cyclical encoding for hours
        # This prevents the ML from thinking Hour 23 is "far" from Hour 0
        df['hour_sin'] = np.sin(2 * np.pi * df['execution_hour'] / 24)
        df['hour_cos'] = np.cos(2 * np.pi * df['execution_hour'] / 24)

        # Drop raw columns that the ML model cannot process (strings, dates, exact prices)
        drop_cols = ['type', 'regime', 'open_time', 'close_time', 'open_price', 'close_price', 'sl', 'tp', 'execution_hour']
        df_ml = df.drop(columns=[col for col in drop_cols if col in df.columns])

        return df_ml

    def build_dataset(self):
        print("\n==================================================")
        print(" 🧠 KOM v1.0 — ML PIPELINE EXTRACTION")
        print("==================================================")
        
        # 1. Extract
        raw_df = self.extract_trade_data()
        if raw_df.empty:
            print("🛑 Extraction Halted: Database is empty or locked.")
            return

        # 2. Vectorize
        ml_ready_df = self.engineer_features(raw_df)

        # 3. Export
        # [BUG-69 FIX] Uses timezone-aware UTC to prevent deprecation warnings
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        export_path = os.path.join(self.export_dir, f"training_matrix_{timestamp}.csv")
        
        # We only export the final model if we have the statistically significant threshold
        if len(ml_ready_df) < 30:
            print(f"\n⚠️ WARNING: N={len(ml_ready_df)}. Kelly/ML Threshold (30) not met.")
            print("Data extracted for preview, but training should NOT commence.")
        else:
            print(f"\n✅ THRESHOLD MET: N={len(ml_ready_df)}. Dataset is statistically significant.")

        ml_ready_df.to_csv(export_path, index=False)
        print(f"💾 ML Matrix saved successfully: {export_path}")
        print("==================================================\n")

if __name__ == "__main__":
    extractor = MLDataExtractor()
    extractor.build_dataset()
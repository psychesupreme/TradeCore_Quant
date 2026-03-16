# ============================================================
# Kom v1.0 — model_trainer.py
# [PHASE C: XGBOOST ALGORITHMIC TRAINING]
#
# PURPOSE:
#   Ingests the latest training_matrix.csv from the media folder,
#   trains an XGBoost binary classifier to predict trade success,
#   and saves the serialized model (.json or .pkl) for the bot 
#   to use during live execution scoring.
# ============================================================

import os
import glob
import pandas as pd
import numpy as np

# Requires: pip install xgboost scikit-learn
try:
    import xgboost as xgb
    from sklearn.model_selection import train_test_split
    from sklearn.metrics import accuracy_score, precision_score, classification_report
    XGB_AVAILABLE = True
except ImportError:
    XGB_AVAILABLE = False

class ModelTrainer:
    def __init__(self, data_dir="media"):
        self.data_dir = data_dir
        self.model_path = os.path.join(self.data_dir, "kom_xgboost_v1.json")

    def get_latest_matrix(self):
        """Finds the most recently generated training matrix in the media folder."""
        search_pattern = os.path.join(self.data_dir, "training_matrix_*.csv")
        files = glob.glob(search_pattern)
        if not files:
            return None
        # Sort by modification time (newest first)
        latest_file = max(files, key=os.path.getctime)
        return latest_file

    def train(self):
        print("\n==================================================")
        print(" 🤖 KOM v1.0 — XGBOOST MODEL TRAINING")
        print("==================================================")

        if not XGB_AVAILABLE:
            print("❌ ERROR: XGBoost or Scikit-Learn not installed.")
            print("   Please run: pip install xgboost scikit-learn")
            return

        csv_file = self.get_latest_matrix()
        if not csv_file:
            print("🛑 Extraction Halted: No training matrix found in /media.")
            return

        print(f"📂 Loading latest matrix: {csv_file}")
        df = pd.read_csv(csv_file)

        # Ensure we have enough data (Hard block)
        if len(df) < 30:
            print(f"\n🛑 CRITICAL HALT: Dataset only contains N={len(df)} rows.")
            print("   XGBoost requires a minimum of N=30 trades to prevent severe curve-fitting.")
            print("   Please allow the live bot to gather more data before training.")
            print("==================================================\n")
            return

        # 1. Feature Selection (Drop identifiers and target variables)
        drop_cols = ['ticket', 'symbol', 'volume', 'profit', 'target_win']
        X = df.drop(columns=[c for c in drop_cols if c in df.columns])
        y = df['target_win']

        # 2. Train/Test Split (80% Train, 20% Test)
        X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

        # 3. Model Definition (Hyperparameters tuned for small/noisy financial datasets)
        print("⚙️ Initializing XGBoost Classifier...")
        model = xgb.XGBClassifier(
            n_estimators=100,        # Number of boosting rounds
            max_depth=3,             # Shallow trees to prevent overfitting
            learning_rate=0.05,      # Conservative learning rate
            subsample=0.8,           # Randomly sample 80% of rows per tree
            colsample_bytree=0.8,    # Randomly sample 80% of columns per tree
            random_state=42,
            eval_metric='logloss'
        )

        # 4. Training
        print("🚀 Training model on historical variance...")
        model.fit(X_train, y_train)

        # 5. Evaluation
        y_pred = model.predict(X_test)
        acc = accuracy_score(y_test, y_pred)
        prec = precision_score(y_test, y_pred, zero_division=0)
        
        print("\n📊 MODEL METRICS (Test Set):")
        print(f"   Accuracy  : {acc * 100:.1f}% (Overall Correctness)")
        print(f"   Precision : {prec * 100:.1f}% (When it predicts a WIN, how often is it right?)")
        
        # 6. Feature Importance (What is driving the algorithm?)
        print("\n🔍 TOP FEATURE IMPORTANCE:")
        importances = model.feature_importances_
        feature_names = X.columns
        feat_df = pd.DataFrame({'Feature': feature_names, 'Importance': importances})
        feat_df = feat_df.sort_values(by='Importance', ascending=False).head(5)
        for _, row in feat_df.iterrows():
            print(f"   - {row['Feature']:<20}: {row['Importance']*100:.1f}%")

        # 7. Serialization (Save the brain)
        model.save_model(self.model_path)
        print(f"\n💾 Model successfully serialized and saved to: {self.model_path}")
        print("==================================================\n")

if __name__ == "__main__":
    trainer = ModelTrainer()
    trainer.train()
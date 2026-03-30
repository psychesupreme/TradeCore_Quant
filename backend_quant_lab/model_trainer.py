# ============================================================
# Kom v1.0 — model_trainer.py
# [SPRINT 23-B: XGBOOST TRAINING WITH STRATIFIED CV]
# [S34-FIX] All emoji replaced with ASCII — Windows subprocess
#           stdout encoding crashes on unicode emoji characters.
# ============================================================

import os
import glob
import json
import pandas as pd
import numpy as np

try:
    import xgboost as xgb
    from sklearn.model_selection import StratifiedKFold, cross_validate
    from sklearn.metrics import accuracy_score, precision_score
    XGB_AVAILABLE = True
except ImportError:
    XGB_AVAILABLE = False

MODEL_DIR  = "media"
MODEL_PATH = os.path.join(MODEL_DIR, "kom_xgboost_v1.json")
META_PATH  = os.path.join(MODEL_DIR, "kom_xgboost_v1_meta.json")


class ModelTrainer:
    def __init__(self, data_dir: str = MODEL_DIR):
        self.data_dir   = data_dir
        self.model_path = MODEL_PATH
        self.meta_path  = META_PATH

    def get_latest_matrix(self) -> str | None:
        files = glob.glob(os.path.join(self.data_dir, "training_matrix_*.csv"))
        return max(files, key=os.path.getctime) if files else None

    def train(self):
        print("\n" + "=" * 54)
        print("  KOM v1.0 -- XGBOOST TRAINING (S23-B)")
        print("=" * 54)

        if not XGB_AVAILABLE:
            print("[ERROR] XGBoost or Scikit-Learn not installed.")
            print("        pip install xgboost scikit-learn")
            return

        csv_file = self.get_latest_matrix()
        if not csv_file:
            print("[ERROR] No training matrix found in /media.")
            print("        Run: python ml_pipeline.py first.")
            return

        print(f"[LOAD] {csv_file}")
        df = pd.read_csv(csv_file)
        n  = len(df)

        if n < 30:
            print(f"[HALT] N={n} < 30. Too few trades for reliable training.")
            return

        # ── Feature / target split ────────────────────────────
        id_cols  = ['ticket', 'symbol', 'profit']
        tgt_col  = 'target_win'
        drop_all = id_cols + [tgt_col]
        X = df.drop(columns=[c for c in drop_all if c in df.columns])
        y = df[tgt_col]

        feature_names = list(X.columns)
        n_pos = int(y.sum())
        n_neg = n - n_pos
        scale_pos = n_neg / n_pos if n_pos > 0 else 1.0

        print(f"  N={n}  |  Wins:{n_pos}  Losses:{n_neg}  "
              f"|  scale_pos_weight:{scale_pos:.2f}")
        print(f"  Features ({len(feature_names)}): {feature_names}")

        # ── Model definition ──────────────────────────────────
        model = xgb.XGBClassifier(
            n_estimators=100,
            max_depth=3,
            learning_rate=0.05,
            subsample=0.8,
            colsample_bytree=0.8,
            scale_pos_weight=scale_pos,
            random_state=42,
            eval_metric='logloss',
            verbosity=0,
        )

        # ── [S23-B-1] Stratified 5-fold CV ───────────────────
        print("\n[CV] Running 5-fold stratified cross-validation...")
        cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
        cv_results = cross_validate(
            model, X, y, cv=cv,
            scoring=['accuracy', 'precision'],
            return_train_score=False,
        )
        acc_mean  = cv_results['test_accuracy'].mean()
        acc_std   = cv_results['test_accuracy'].std()
        prec_mean = cv_results['test_precision'].mean()
        prec_std  = cv_results['test_precision'].std()

        print(f"\n[METRICS] CROSS-VALIDATION (5-fold, N={n}):")
        print(f"   Accuracy  : {acc_mean*100:.1f}% +/- {acc_std*100:.1f}%")
        print(f"   Precision : {prec_mean*100:.1f}% +/- {prec_std*100:.1f}%")

        if prec_mean >= 0.60:
            gate_note = "[OK]   Precision >= 60% -- model adds value to execution gate."
        elif prec_mean >= 0.50:
            gate_note = "[WARN] Precision 50-60% -- marginal edge, small adjustment applied."
        else:
            gate_note = "[FAIL] Precision < 50% -- model worse than random. Scoring disabled."
        print(f"   {gate_note}")

        # ── Final fit on full dataset ─────────────────────────
        print("\n[FIT] Fitting final model on full dataset...")
        model.fit(X, y)

        # ── Feature importance ────────────────────────────────
        importances = model.feature_importances_
        feat_df = pd.DataFrame({'feature': feature_names, 'importance': importances})
        feat_df = feat_df.sort_values('importance', ascending=False)
        print("\n[TOP 8 FEATURES]")
        for _, row in feat_df.head(8).iterrows():
            bar = "#" * int(row['importance'] * 40)
            print(f"   {row['feature']:<22} {row['importance']*100:5.1f}%  {bar}")

        # ── Save model + metadata ─────────────────────────────
        os.makedirs(self.data_dir, exist_ok=True)
        model.save_model(self.model_path)

        meta = {
            'feature_names':    feature_names,
            'n_trades':         n,
            'cv_accuracy':      round(acc_mean, 4),
            'cv_accuracy_std':  round(acc_std, 4),
            'cv_precision':     round(prec_mean, 4),
            'cv_precision_std': round(prec_std, 4),
            'precision_gate':   'ACTIVE' if prec_mean >= 0.50 else 'DISABLED',
            'trained_at':       pd.Timestamp.now('UTC').isoformat(),
        }
        with open(self.meta_path, 'w') as f:
            json.dump(meta, f, indent=2)

        print(f"\n[SAVE] Model : {self.model_path}")
        print(f"       Meta  : {self.meta_path}")
        print("=" * 54 + "\n")
        return model, meta


class LiveScorer:
    """
    [S23-C] Loads the trained XGBoost model and provides
    score_trade() for bot_engine to call during execution.
    Returns a confidence adjustment in [-0.05, +0.05].
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._loaded = False
        return cls._instance

    def _load(self):
        if self._loaded:
            return
        try:
            if not os.path.exists(MODEL_PATH) or not os.path.exists(META_PATH):
                self._model    = None
                self._meta     = {}
                self._features = []
                self._active   = False
                return
            self._model = xgb.XGBClassifier()
            self._model.load_model(MODEL_PATH)
            with open(META_PATH) as f:
                self._meta = json.load(f)
            self._features = self._meta.get('feature_names', [])
            self._active   = self._meta.get('precision_gate') == 'ACTIVE'
            self._loaded   = True
        except Exception as e:
            print(f"[WARN] LiveScorer load error: {e}")
            self._model  = None
            self._active = False

    def is_active(self) -> bool:
        self._load()
        return bool(self._active and self._model is not None)

    def score_trade(self, features: dict) -> float:
        self._load()
        if not self.is_active():
            return 0.0
        try:
            row = pd.DataFrame([features])
            for col in self._features:
                if col not in row.columns:
                    row[col] = 0.0
            row = row[self._features]
            proba = self._model.predict_proba(row)[0][1]
            if   proba >= 0.65: return  0.05
            elif proba >= 0.55: return  0.02
            elif proba >= 0.45: return  0.00
            elif proba >= 0.35: return -0.02
            else:               return -0.05
        except Exception:
            return 0.0


if __name__ == "__main__":
    trainer = ModelTrainer()
    trainer.train()

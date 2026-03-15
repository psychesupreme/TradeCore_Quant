# ============================================================
# Kom v1.0 (formerly TradeCore) — quant_analyzer.py 
# [SPRINT 18a: REBRAND & ELITE 10 ALIGNMENT]
#
# SPRINT 18a UPGRADES:
#   - System rebranded to Kom v1.0.
#   - CLI Output updated to reflect Kom v1.0 architecture.
#   - Asset Breakdown filters updated to map to the Elite 10 Matrix.
#
# HISTORICAL PRESERVATION (Sprint 7 Full Rewrite):
#   Expectancy          — per-trade and per-dollar-risked EV
#   Sharpe Ratio        — risk-adjusted return (annualised)
#   Sortino Ratio       — downside-only deviation penalty
#   Calmar Ratio        — CAGR / max drawdown
#   VaR (99%)           — parametric ATR-based (Sprint 6)
#   CVaR                — Expected Shortfall beyond VaR tail
#   MAE / MFE           — excursion analysis per trade
#   Kelly Criterion     — optimal risk fraction (quarter-Kelly)
#   Monte Carlo         — 10,000 bootstrap equity path simulations
#   Markov Chain        — 4-state regime transition matrix
#   QML (stub)          — activates at N >= 30 trades
#
# PRIMARY INTERFACE for bot_engine:
#   QuantEngine.get_live_risk_params() -> dict
#   Called every run_cycle. Returns risk_pct, var_limit, cvar_limit,
#   kelly_fraction, regime_gate, regime_multiplier.
#   Internally cached for 5 minutes to avoid DB hammering.
# ============================================================

import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from db_manager import DBManager


# ── GRADING HELPERS ───────────────────────────────────────────────────────────

def _grade(value, thresholds: dict) -> str:
    """Returns a letter grade given threshold dict {grade: min_value}."""
    for grade, min_val in sorted(thresholds.items(),
                                 key=lambda x: x[1], reverse=True):
        if value >= min_val:
            return grade
    return list(thresholds.keys())[-1]


# ── MAIN ENGINE CLASS ──────────────────────────────────────────────────────────

class QuantEngine:
    """
    Full quantitative analytics engine. Reads from tradecore.db.
    All public methods return structured dicts ready for logging,
    dashboard display, or bot_engine risk gating.
    """

    def __init__(self):
        self._cache: dict   = {}
        self._cache_time    = None
        self._cache_ttl     = timedelta(minutes=5)

    # ─────────────────────────────────────────────────────────
    # LIVE RISK PARAMS — called by bot_engine every cycle
    # ─────────────────────────────────────────────────────────

    def get_live_risk_params(self) -> dict:
        """
        Fast cached accessor. Returns everything bot_engine needs
        to size positions, gate trading, and set kill switch levels.

        Returns:
            risk_pct          — recommended % of balance to risk per trade
            var_limit         — 99% VaR in USD (kill stage 2)
            cvar_limit        — CVaR in USD (kill stage 1 — fires first)
            kelly_fraction    — quarter-Kelly risk fraction
            regime_gate       — 'NORMAL' | 'REDUCE' | 'HALT'
            regime_multiplier — 1.0 | 0.5 | 0.0 (applied to risk_pct)
            n_trades          — number of closed trades in sample
            statistically_valid — True if N >= 30
        """
        now = datetime.utcnow()
        if self._cache_time and (now - self._cache_time) < self._cache_ttl:
            return self._cache

        df    = DBManager.get_closed_trades()
        eq    = DBManager.get_equity_curve()
        n     = len(df)
        valid = n >= 30

        # VaR / CVaR from equity curve
        var_result  = self._compute_var_cvar(eq)
        var_limit   = var_result.get('var_usd', 0.0)
        cvar_limit  = var_result.get('cvar_usd', 0.0)

        # Kelly
        kelly_result = self.kelly(df)
        kelly_frac   = kelly_result.get('quarter', 0.02)  # floor at 2% if N<30

        # Markov regime
        markov_result  = self.markov_regime(eq)
        regime_gate    = markov_result.get('trading_gate', 'NORMAL')
        regime_mult    = {'NORMAL': 1.0, 'REDUCE': 0.5, 'HALT': 0.0}.get(regime_gate, 1.0)

        # Conservative risk: use Kelly only once statistically valid
        risk_pct = kelly_frac if valid else min(0.02, kelly_frac)

        result = {
            'risk_pct':           round(risk_pct, 4),
            'var_limit':          round(var_limit, 2),
            'cvar_limit':         round(cvar_limit, 2),
            'kelly_fraction':     round(kelly_frac, 4),
            'regime_gate':        regime_gate,
            'regime_multiplier':  regime_mult,
            'n_trades':           n,
            'statistically_valid': valid,
        }
        self._cache      = result
        self._cache_time = now
        return result

    # ─────────────────────────────────────────────────────────
    # FULL REPORT — called by portfolio_tracker / on demand
    # ─────────────────────────────────────────────────────────

    def full_report(self) -> dict:
        """
        Runs all metrics and returns a single structured dict.
        Use this for the Flutter dashboard performance panel.
        """
        df  = DBManager.get_closed_trades()
        eq  = DBManager.get_equity_curve()
        n   = len(df)

        report = {
            'n_trades':         n,
            'valid':            n >= 30,
            'generated_at':     datetime.utcnow().isoformat(),
            'expectancy':       self.expectancy(df),
            'sharpe':           self.sharpe(eq),
            'sortino':          self.sortino(eq),
            'calmar':           self.calmar(eq),
            'var_cvar':         self._compute_var_cvar(eq),
            'kelly':            self.kelly(df),
            'mae_mfe':          self.mae_mfe(df),
            'monte_carlo':      self.monte_carlo(df),
            'markov':           self.markov_regime(eq),
            'qml':              self.qml_signal_quality(df),
            'drawdown':         self._drawdown_stats(eq),
            'asset_breakdown':  self._asset_breakdown(df),
        }
        return report

    # ─────────────────────────────────────────────────────────
    # EXPECTANCY
    # ─────────────────────────────────────────────────────────

    def expectancy(self, df: pd.DataFrame = None) -> dict:
        """
        E = (WR × avg_win) - (LR × avg_loss)
        Per-dollar: E / avg_loss  (normalises for position size)

        Grading: A >= 0.50/dollar, B >= 0.30, C >= 0.10, D < 0.10
        """
        if df is None:
            df = DBManager.get_closed_trades()
        if df.empty:
            return {'value': 0.0, 'per_dollar': 0.0, 'grade': 'N/A', 'n': 0}

        wins   = df[df['profit'] > 0]['profit']
        losses = df[df['profit'] < 0]['profit']
        n      = len(df)
        wr     = len(wins) / n
        lr     = 1.0 - wr

        avg_win  = wins.mean()  if len(wins)   > 0 else 0.0
        avg_loss = abs(losses.mean()) if len(losses) > 0 else 1.0

        ev         = (wr * avg_win) - (lr * avg_loss)
        per_dollar = ev / avg_loss if avg_loss > 0 else 0.0

        return {
            'value':     round(ev, 2),
            'per_dollar': round(per_dollar, 3),
            'avg_win':   round(avg_win, 2),
            'avg_loss':  round(avg_loss, 2),
            'win_rate':  round(wr, 4),
            'grade':     _grade(per_dollar, {'A':0.50,'B':0.30,'C':0.10,'D':0.0}),
            'n':         n,
        }

    # ─────────────────────────────────────────────────────────
    # SHARPE RATIO
    # ─────────────────────────────────────────────────────────

    def sharpe(self, eq: pd.DataFrame = None, rf_annual: float = 0.05) -> dict:
        """
        S = (mean_daily_return - Rf_daily) / std_daily_return × sqrt(252)
        rf_annual defaults to 5% (US T-bill rate as of 2026).
        Uses daily balance returns resampled from minute snapshots.

        Grading: A >= 2.0, B >= 1.5, C >= 1.0, D < 1.0
        Note: punishes ALL volatility equally — use Sortino for asymmetric profiles.
        """
        if eq is None:
            eq = DBManager.get_equity_curve()
        daily = self._daily_returns(eq)
        if daily is None or len(daily) < 5:
            return {'ratio': None, 'grade': 'N/A (< 5 days data)',
                    'annualised': None, 'mean_daily': None, 'std_daily': None}

        rf_daily = rf_annual / 252
        excess   = daily - rf_daily
        ratio    = (excess.mean() / excess.std()) * np.sqrt(252) if excess.std() > 0 else 0.0

        return {
            'ratio':       round(ratio, 3),
            'annualised':  round(ratio, 3),
            'mean_daily':  round(daily.mean(), 5),
            'std_daily':   round(daily.std(), 5),
            'grade':       _grade(ratio, {'A':2.0,'B':1.5,'C':1.0,'D':0.0}),
        }

    # ─────────────────────────────────────────────────────────
    # SORTINO RATIO
    # ─────────────────────────────────────────────────────────

    def sortino(self, eq: pd.DataFrame = None, rf_annual: float = 0.05) -> dict:
        """
        So = (mean_daily_return - Rf_daily) / downside_deviation × sqrt(252)
        downside_deviation = std of returns WHERE return < target (MAR = Rf)
        Better than Sharpe for strategies with right-skewed returns (like ours).
        The $702 XAGUSD win helps Sortino; Sharpe penalises it as vol.

        Grading: A >= 3.0, B >= 2.0, C >= 1.5, D < 1.5
        """
        if eq is None:
            eq = DBManager.get_equity_curve()
        daily = self._daily_returns(eq)
        if daily is None or len(daily) < 5:
            return {'ratio': None, 'grade': 'N/A (< 5 days data)',
                    'downside_std': None}

        rf_daily  = rf_annual / 252
        downside  = daily[daily < rf_daily] - rf_daily
        down_std  = np.sqrt((downside**2).mean()) if len(downside) > 0 else 1e-9
        ratio     = ((daily.mean() - rf_daily) / down_std) * np.sqrt(252)

        return {
            'ratio':        round(ratio, 3),
            'downside_std': round(down_std, 5),
            'grade':        _grade(ratio, {'A':3.0,'B':2.0,'C':1.5,'D':0.0}),
        }

    # ─────────────────────────────────────────────────────────
    # CALMAR RATIO
    # ─────────────────────────────────────────────────────────

    def calmar(self, eq: pd.DataFrame = None) -> dict:
        """
        C = CAGR / |max_drawdown_pct|
        CAGR = (final / initial)^(365/days) - 1
        Protects against strategies with great returns but one catastrophic DD.

        Grading: A >= 10, B >= 3, C >= 1, D < 1
        """
        if eq is None:
            eq = DBManager.get_equity_curve()
        dd = self._drawdown_stats(eq)
        if dd['max_dd_pct'] is None or dd['max_dd_pct'] == 0:
            return {'ratio': None, 'grade': 'N/A', 'cagr': None,
                    'max_dd_pct': None}

        cagr_result = self._cagr(eq)
        cagr_val    = cagr_result.get('cagr', 0.0) or 0.0
        max_dd      = abs(dd['max_dd_pct'])
        ratio       = cagr_val / max_dd if max_dd > 0 else 0.0

        return {
            'ratio':      round(ratio, 3),
            'cagr':       round(cagr_val, 4),
            'max_dd_pct': round(max_dd, 4),
            'grade':      _grade(ratio, {'A':10.0,'B':3.0,'C':1.0,'D':0.0}),
        }

    # ─────────────────────────────────────────────────────────
    # VAR + CVAR (Expected Shortfall)
    # ─────────────────────────────────────────────────────────

    def _compute_var_cvar(self, eq: pd.DataFrame = None) -> dict:
        """
        VaR(99%) = balance × daily_return_std × z(99%) × 2.326
        CVaR(99%) = VaR × (phi(z) / (1-alpha)) / (1/sqrt(2π))
                  ≈ VaR × 1.29  for normal distribution
        CVaR is always >= VaR. It represents the AVERAGE loss
        in the worst 1% of sessions — more conservative kill trigger.

        Uses historical simulation when N>=20 days (preferred).
        Falls back to parametric (ATR-based) when insufficient data.
        """
        if eq is None:
            eq = DBManager.get_equity_curve()

        daily  = self._daily_returns(eq)
        latest = eq.tail(1)
        balance = float(latest['balance'].iloc[0]) if not latest.empty else 10000.0

        if daily is not None and len(daily) >= 20:
            # Historical simulation VaR (non-parametric, preferred)
            sorted_losses = np.sort(daily.values)          # ascending (worst first)
            alpha         = 0.01                           # 99% confidence
            var_idx       = int(np.ceil(alpha * len(sorted_losses))) - 1
            var_idx       = max(0, var_idx)
            var_pct       = abs(sorted_losses[var_idx])    # positive number
            cvar_pct      = abs(sorted_losses[:var_idx+1].mean()) if var_idx >= 0 else var_pct * 1.29
        else:
            # Parametric fallback
            if daily is not None and len(daily) >= 3:
                std_daily = daily.std()
            else:
                std_daily = 0.003                          # conservative default
            z_99     = 2.326
            var_pct  = std_daily * z_99
            cvar_pct = var_pct * 1.29                      # normal dist approximation

        var_usd  = balance * var_pct
        cvar_usd = balance * cvar_pct

        # Apply floor/ceiling: [1.5%, 20%]
        var_usd  = max(balance * 0.015, min(var_usd,  balance * 0.20))
        cvar_usd = max(balance * 0.020, min(cvar_usd, balance * 0.25))

        return {
            'var_pct':   round(var_pct, 4),
            'cvar_pct':  round(cvar_pct, 4),
            'var_usd':   round(var_usd, 2),
            'cvar_usd':  round(cvar_usd, 2),
            'balance':   round(balance, 2),
            'method':    'historical' if (daily is not None and len(daily) >= 20)
                         else 'parametric',
        }

    # ─────────────────────────────────────────────────────────
    # MAE / MFE EXCURSION ANALYSIS
    # ─────────────────────────────────────────────────────────

    def mae_mfe(self, df: pd.DataFrame = None) -> dict:
        """
        MAE = Max Adverse Excursion (worst floating loss before close)
        MFE = Max Favorable Excursion (best floating gain before close)

        Key ratios:
          mae_sl_ratio  = avg_mae / avg_sl_distance
            > 0.80: stops too tight — frequently hit before TP
            < 0.30: stops too wide — excess capital at risk
            IDEAL:  0.40–0.60

          mfe_tp_ratio  = avg_mfe / avg_tp_distance
            < 0.80: price rarely reaches TP — TP too ambitious
            > 1.50: TP is being consistently hit and price continues — widen TP
            IDEAL:  0.80–1.20
        """
        if df is None:
            df = DBManager.get_closed_trades()
        if df.empty or 'mae' not in df.columns:
            return {'available': False, 'n': 0}

        df_valid = df[(df['mae'] > 0) | (df['mfe'] > 0)].copy()
        if df_valid.empty:
            return {'available': False, 'n': 0,
                    'note': 'MAE/MFE tracking started Sprint 7 — no data yet'}

        avg_mae = df_valid['mae'].mean()
        avg_mfe = df_valid['mfe'].mean()
        max_mae = df_valid['mae'].max()
        max_mfe = df_valid['mfe'].max()

        # SL and TP distances from open price
        df_valid['sl_dist'] = abs(df_valid['open_price'] - df_valid['sl'].fillna(df_valid['open_price']))
        df_valid['tp_dist'] = abs(df_valid['tp'].fillna(df_valid['close_price']) - df_valid['open_price'])

        avg_sl = df_valid['sl_dist'].mean()
        avg_tp = df_valid['tp_dist'].mean()

        mae_sl_ratio = (avg_mae / avg_sl) if avg_sl > 0 else None
        mfe_tp_ratio = (avg_mfe / avg_tp) if avg_tp > 0 else None

        def sl_assessment(r):
            if r is None: return 'Insufficient data'
            if r > 0.80:  return '⚠️ Stops too tight — frequently hit before TP'
            if r < 0.30:  return '⚠️ Stops too wide — excess capital at risk'
            return '✅ Stop placement healthy'

        def tp_assessment(r):
            if r is None: return 'Insufficient data'
            if r < 0.80:  return '⚠️ TP too ambitious — price rarely reaches target'
            if r > 1.50:  return '💡 TP too conservative — price runs past target'
            return '✅ TP placement healthy'

        return {
            'available':     True,
            'n':             len(df_valid),
            'avg_mae':       round(avg_mae, 5),
            'avg_mfe':       round(avg_mfe, 5),
            'max_mae':       round(max_mae, 5),
            'max_mfe':       round(max_mfe, 5),
            'mae_sl_ratio':  round(mae_sl_ratio, 3) if mae_sl_ratio else None,
            'mfe_tp_ratio':  round(mfe_tp_ratio, 3) if mfe_tp_ratio else None,
            'sl_assessment': sl_assessment(mae_sl_ratio),
            'tp_assessment': tp_assessment(mfe_tp_ratio),
        }

    # ─────────────────────────────────────────────────────────
    # KELLY CRITERION
    # ─────────────────────────────────────────────────────────

    def kelly(self, df: pd.DataFrame = None) -> dict:
        """
        f* = (b×p - q) / b
        b = avg_win / avg_loss  (win:loss ratio)
        p = win rate, q = 1 - p

        Full Kelly is theoretically optimal but causes catastrophic drawdowns
        in practice because it assumes the distribution is known perfectly.
        With N<50 trades, estimation error alone makes full Kelly dangerous.

        Recommended usage:
          N <  30: use fixed 2% regardless
          N >= 30: use quarter-Kelly, capped at 3%
          N >= 100: use half-Kelly, capped at 4%
        """
        if df is None:
            df = DBManager.get_closed_trades()
        n = len(df)

        if n < 5:
            return {'full': None, 'half': None, 'quarter': 0.02,
                    'recommended_risk_pct': 0.02,
                    'note': f'N={n} — need N>=30 for reliable Kelly. Using 2% fixed.'}

        wins   = df[df['profit'] > 0]['profit']
        losses = df[df['profit'] < 0]['profit']

        if len(wins) == 0 or len(losses) == 0:
            return {'full': None, 'half': None, 'quarter': 0.02,
                    'recommended_risk_pct': 0.02,
                    'note': 'All wins or all losses — Kelly undefined'}

        b  = wins.mean() / abs(losses.mean())
        p  = len(wins) / n
        q  = 1.0 - p
        f  = (b * p - q) / b
        f  = max(0.0, f)   # Kelly is negative when system has no edge — use 0

        full_k    = round(f, 4)
        half_k    = round(f / 2, 4)
        quarter_k = round(f / 4, 4)

        if n >= 100:
            rec = min(0.04, half_k)
            note = f'N={n} ≥ 100 — half-Kelly capped at 4%'
        elif n >= 30:
            rec = min(0.03, quarter_k)
            note = f'N={n} ≥ 30 — quarter-Kelly capped at 3%'
        else:
            rec = 0.02
            note = f'N={n} < 30 — quarter-Kelly computed but 2% fixed rate used'

        return {
            'full':                round(full_k, 4),
            'half':                round(half_k, 4),
            'quarter':             round(quarter_k, 4),
            'recommended_risk_pct': round(rec, 4),
            'b_ratio':             round(b, 3),
            'win_rate':            round(p, 4),
            'note':                note,
            'n':                   n,
        }

    # ─────────────────────────────────────────────────────────
    # MONTE CARLO SIMULATION
    # ─────────────────────────────────────────────────────────

    def monte_carlo(self, df: pd.DataFrame = None,
                    n_simulations: int = 10_000,
                    n_trades_forward: int = 100) -> dict:
        """
        Bootstrap resampling Monte Carlo — no normality assumption.
        Randomly samples from historical trade outcomes (with replacement)
        to simulate future equity paths.

        Why bootstrap not parametric:
          Trade P&L is NOT normally distributed. FX returns have fat tails.
          Resampling from actual outcomes preserves the real distribution shape
          including skew, kurtosis, and outlier ($702) trades.

        With N=7 trades: intervals are very wide. That's honest — we don't
        pretend to have more certainty than the data supports.
        """
        if df is None:
            df = DBManager.get_closed_trades()
        n = len(df)

        if n < 5:
            return {
                'available': False,
                'n': n,
                'note': f'Need N>=5 for Monte Carlo. Have N={n}.'
            }

        profits     = df['profit'].values
        latest_bal  = DBManager.get_equity_curve()
        start_bal   = float(latest_bal['balance'].iloc[-1]) if not latest_bal.empty else 10000.0

        rng         = np.random.default_rng(seed=42)
        final_equities = np.zeros(n_simulations)
        max_drawdowns  = np.zeros(n_simulations)
        ruin_threshold = start_bal * 0.50  # define ruin as losing 50% of balance

        for i in range(n_simulations):
            # Sample n_trades_forward outcomes with replacement
            sampled    = rng.choice(profits, size=n_trades_forward, replace=True)
            equity_path = np.cumsum(sampled) + start_bal

            final_equities[i] = equity_path[-1]

            # Max drawdown for this path
            peak = np.maximum.accumulate(equity_path)
            dd   = (peak - equity_path)
            max_drawdowns[i] = dd.max()

        p_ruin   = np.mean(final_equities < ruin_threshold)
        p_double = np.mean(final_equities > start_bal * 2.0)

        pct = np.percentile(final_equities, [5, 25, 50, 75, 95])
        expected_max_dd = np.percentile(max_drawdowns, 95)  # 95th percentile worst DD

        return {
            'available':         True,
            'n_trades_used':     n,
            'n_simulations':     n_simulations,
            'n_forward':         n_trades_forward,
            'start_balance':     round(start_bal, 2),
            'p_ruin':            round(p_ruin, 4),        # P(lose 50%)
            'p_double':          round(p_double, 4),       # P(2x account)
            'p5_equity':         round(pct[0], 2),         # worst 5% outcome
            'p25_equity':        round(pct[1], 2),
            'p50_equity':        round(pct[2], 2),         # median outcome
            'p75_equity':        round(pct[3], 2),
            'p95_equity':        round(pct[4], 2),         # best 5% outcome
            'expected_max_dd':   round(expected_max_dd, 2),
            'wide_intervals_note': 'N<30: confidence intervals are wide by design' if n < 30 else None,
        }

    # ─────────────────────────────────────────────────────────
    # MARKOV CHAIN REGIME DETECTION
    # ─────────────────────────────────────────────────────────

    def markov_regime(self, eq: pd.DataFrame = None) -> dict:
        """
        4-state Markov chain built from rolling ATR% of equity/balance curve.
        States: BULL (trending up), BEAR (trending down), RANGE, HIGH_VOL

        Transition matrix T[i][j] = P(moving to state j | currently in state i)
        Built from observed state sequences in account_snapshots.

        Trading gates:
          NORMAL  (P(HIGH_VOL) < 30% and P(BEAR) < 40%)
          REDUCE  (P(HIGH_VOL) 30–50% or P(BEAR) 40–65%) → half position size
          HALT    (P(HIGH_VOL) > 50% or P(BEAR) > 65%)    → no new trades

        With limited data (< 3 days): returns NORMAL with low confidence.
        """
        if eq is None:
            eq = DBManager.get_equity_curve()

        if eq.empty or len(eq) < 30:
            return {
                'current_state':    'NORMAL',
                'trading_gate':     'NORMAL',
                'confidence':       'low',
                'note':             'Insufficient equity history for Markov analysis',
                'transition_matrix': None,
            }

        # Use BALANCE not equity — equity bounces with every open position tick
        # which makes an active account look permanently HIGH_VOL.
        # Balance only changes on trade close, giving a clean regime signal.
        eq = eq.copy().set_index('timestamp').resample('1min').last().ffill()
        eq['ret'] = eq['balance'].pct_change().fillna(0)

        # Rolling 30-minute vol for regime labelling (wider window = less noise)
        eq['vol'] = eq['ret'].rolling(30).std().fillna(0)
        eq['trend'] = eq['balance'].rolling(30).mean().diff().fillna(0)

        # Label each minute with a state
        def label_state(row):
            vol_threshold = eq['vol'].quantile(0.90)  # top 10% only
            if row['vol'] > vol_threshold and vol_threshold > 0:
                return 'HIGH_VOL'
            if row['trend'] > eq['vol'].mean() * 1.0:
                return 'BULL'
            if row['trend'] < -eq['vol'].mean() * 1.0:
                return 'BEAR'
            return 'RANGE'

        eq['state'] = eq.apply(label_state, axis=1)
        states      = eq['state'].tolist()
        state_labels = ['BULL', 'BEAR', 'RANGE', 'HIGH_VOL']
        idx_map      = {s: i for i, s in enumerate(state_labels)}

        # Build transition count matrix
        n_states = len(state_labels)
        counts   = np.zeros((n_states, n_states))
        for a, b in zip(states[:-1], states[1:]):
            if a in idx_map and b in idx_map:
                counts[idx_map[a]][idx_map[b]] += 1

        # Normalise rows → probability matrix
        row_sums = counts.sum(axis=1, keepdims=True)
        row_sums[row_sums == 0] = 1  # avoid div/0
        T = counts / row_sums

        # Current state and one-step forward probabilities
        current_state   = states[-1] if states else 'RANGE'
        current_idx     = idx_map.get(current_state, 2)
        forward_probs   = T[current_idx]
        p_high_vol      = forward_probs[idx_map['HIGH_VOL']]
        p_bear          = forward_probs[idx_map['BEAR']]

        # Gate decision
        n_obs = len(states)
        if n_obs < 200:
            gate = 'NORMAL'   # not enough history to trust Markov output
        elif p_high_vol > 0.70 or p_bear > 0.75:
            gate = 'HALT'
        elif p_high_vol > 0.50 or p_bear > 0.55:
            gate = 'REDUCE'
        else:
            gate = 'NORMAL'

        T_readable = {
            state_labels[i]: {state_labels[j]: round(T[i][j], 3)
                               for j in range(n_states)}
            for i in range(n_states)
        }

        return {
            'current_state':     current_state,
            'p_high_vol_next':   round(float(p_high_vol), 3),
            'p_bear_next':       round(float(p_bear), 3),
            'trading_gate':      gate,
            'confidence':        'high' if len(states) > 500 else 'medium' if len(states) > 100 else 'low',
            'transition_matrix': T_readable,
            'n_observations':    len(states),
        }

    # ─────────────────────────────────────────────────────────
    # QML SIGNAL QUALITY (activates at N >= 30)
    # ─────────────────────────────────────────────────────────

    def qml_signal_quality(self, df: pd.DataFrame = None) -> dict:
        """
        Quantile Machine Learning: learns which signal features
        predict bottom-10% (losers) vs top-10% (winners) outcomes.

        Pre-requisite: N >= 30 closed trades with ICT scores logged.
        Until then, returns {'available': False} and bot_engine
        runs on pure ICT confluence scoring.

        When active: returns score_adjustment per feature combination,
        used to bump or penalise raw ICT confidence by ±0.03 before
        the execution threshold comparison.
        """
        if df is None:
            df = DBManager.get_closed_trades()
        n = len(df)

        if n < 30:
            return {
                'available':  False,
                'n':          n,
                'needed':     30 - n,
                'note':       f'QML activates at N=30. Currently N={n}. '
                              f'Collecting training data — {30-n} more trades needed.',
            }

        # N >= 30: quantile regression pending Phase C implementation
        return {
            'available':  False,
            'n':          n,
            'note':       f'QML training pending (Phase C). N={n} ≥ 30 — '
                          f'data collecting. Execution uses ICT scores only.',
            'score_adjustment': 0.0,
        }

    # ─────────────────────────────────────────────────────────
    # INTERNAL HELPERS
    # ─────────────────────────────────────────────────────────

    def _daily_returns(self, eq: pd.DataFrame) -> pd.Series | None:
        """Resample minute snapshots to daily, return % balance returns."""
        if eq is None or eq.empty or len(eq) < 2:
            return None
        try:
            eq = eq.copy().set_index('timestamp')
            daily_bal = eq['balance'].resample('1D').last().dropna()
            if len(daily_bal) < 2:
                return None
            returns = daily_bal.pct_change().dropna()
            return returns
        except Exception:
            return None

    def _cagr(self, eq: pd.DataFrame) -> dict:
        if eq is None or eq.empty or len(eq) < 2:
            return {'cagr': None}
        try:
            first = float(eq['balance'].iloc[0])
            last  = float(eq['balance'].iloc[-1])
            days  = (eq['timestamp'].iloc[-1] - eq['timestamp'].iloc[0]).days
            if days < 1 or first <= 0:
                return {'cagr': None}
            cagr = (last / first) ** (365 / days) - 1
            return {'cagr': round(cagr, 4), 'days': days,
                    'initial': round(first, 2), 'final': round(last, 2)}
        except Exception:
            return {'cagr': None}

    def _drawdown_stats(self, eq: pd.DataFrame) -> dict:
        if eq is None or eq.empty:
            return {'max_dd_usd': None, 'max_dd_pct': None, 'current_dd_pct': None}
        try:
            bal       = eq['balance'].values
            peak      = np.maximum.accumulate(bal)
            dd_usd    = peak - bal
            dd_pct    = dd_usd / peak
            max_dd    = dd_pct.max()
            cur_dd    = dd_pct[-1]
            return {
                'max_dd_usd':     round(float(dd_usd.max()), 2),
                'max_dd_pct':     round(float(max_dd), 4),
                'current_dd_pct': round(float(cur_dd), 4),
            }
        except Exception:
            return {'max_dd_usd': None, 'max_dd_pct': None, 'current_dd_pct': None}

    def _asset_breakdown(self, df: pd.DataFrame) -> dict:
        """
        [SPRINT 18a] Updated to map perfectly to the Elite 10 Matrix.
        Groups assets by their institutional class (Hard Assets vs Indices vs FX vs Crypto).
        """
        if df is None or df.empty:
            return {}
        breakdown = {}
        categories = {
            'LIMIT':  df[df['comment'].str.contains('Limit', case=False, na=False)],
            'NANO':   df[df['comment'].str.contains('Nano',  case=False, na=False)],
            'HARD_ASSET': df[df['symbol'].str.contains('XAU|XAG|Oil', na=False)],
            'EQUITIES': df[df['symbol'].str.contains('SP 500|Tech 100', na=False)],
            'FX':     df[df['symbol'].str.contains('USDJPY|EURUSD|GBPUSD', na=False)],
            'CRYPTO': df[df['symbol'].str.contains('BTC|ETH', na=False)],
        }
        for label, subset in categories.items():
            if subset.empty:
                continue
            wins = subset[subset['profit'] > 0]
            loss = subset[subset['profit'] < 0]
            breakdown[label] = {
                'n':          len(subset),
                'win_rate':   round(len(wins)/len(subset), 4),
                'net_pnl':    round(subset['profit'].sum(), 2),
                'avg_win':    round(wins['profit'].mean(), 2) if len(wins) else 0.0,
                'avg_loss':   round(loss['profit'].mean(), 2) if len(loss) else 0.0,
            }
        return breakdown


# ── STANDALONE CLI ─────────────────────────────────────────────────────────────

def analyze_performance():
    """Called when running: python quant_analyzer.py"""
    qe     = QuantEngine()
    report = qe.full_report()
    n      = report['n_trades']

    print("\n" + "="*65)
    print("  📊 KOM v1.0 — QUANTITATIVE PERFORMANCE REPORT")
    print("="*65)
    print(f"  Trades in sample : {n}  ({'statistically valid' if n>=30 else 'OBSERVATION MODE — need 30+'})")
    print(f"  Generated        : {report['generated_at'][:19]} UTC")
    print("="*65)

    e = report['expectancy']
    print(f"\n  EXPECTANCY          ${e.get('value','N/A')}/trade  |  "
          f"${e.get('per_dollar','N/A')}/dollar  |  Grade: {e.get('grade','N/A')}")

    s = report['sharpe']
    print(f"  SHARPE RATIO        {s.get('ratio','N/A')}  |  Grade: {s.get('grade','N/A')}")

    so = report['sortino']
    print(f"  SORTINO RATIO       {so.get('ratio','N/A')}  |  Grade: {so.get('grade','N/A')}")

    c = report['calmar']
    print(f"  CALMAR RATIO        {c.get('ratio','N/A')}  |  CAGR: {c.get('cagr','N/A')}")

    vc = report['var_cvar']
    print(f"  VaR(99%) / CVaR     ${vc.get('var_usd','N/A')} / ${vc.get('cvar_usd','N/A')}  "
          f"|  Method: {vc.get('method','N/A')}")

    k = report['kelly']
    print(f"  KELLY FRACTION      Full: {k.get('full','N/A')}  "
          f"Quarter: {k.get('quarter','N/A')}  "
          f"→ Use: {k.get('recommended_risk_pct','N/A')}")

    mf = report['mae_mfe']
    if mf.get('available'):
        print(f"  MAE/MFE             {mf.get('sl_assessment','N/A')}")
        print(f"                      {mf.get('tp_assessment','N/A')}")
    else:
        print(f"  MAE/MFE             {mf.get('note', 'Tracking starts Sprint 7')}")

    mc = report['monte_carlo']
    if mc.get('available'):
        print(f"  MONTE CARLO         P(ruin): {mc.get('p_ruin','N/A')*100:.1f}%  "
              f"P(2x): {mc.get('p_double','N/A')*100:.1f}%  "
              f"Median: ${mc.get('p50_equity','N/A')}")
    else:
        print(f"  MONTE CARLO         {mc.get('note','N/A')}")

    mk = report['markov']
    print(f"  MARKOV REGIME       {mk.get('current_state','N/A')}  "
          f"|  Gate: {mk.get('trading_gate','N/A')}  "
          f"|  P(BEAR next): {mk.get('p_bear_next','N/A')}")

    qml = report['qml']
    print(f"  QML PIPELINE        {'Active' if qml.get('available') else qml.get('note','N/A')}")

    dd = report['drawdown']
    print(f"\n  MAX DRAWDOWN        ${dd.get('max_dd_usd','N/A')}  "
          f"({(dd.get('max_dd_pct') or 0)*100:.2f}%)")

    print("\n  ELITE 10 BREAKDOWN")
    for label, data in report.get('asset_breakdown', {}).items():
        print(f"    {label:<10}  N={data['n']}  "
              f"WR={data['win_rate']*100:.0f}%  "
              f"Net=${data['net_pnl']}")

    print("="*65 + "\n")


if __name__ == "__main__":
    analyze_performance()
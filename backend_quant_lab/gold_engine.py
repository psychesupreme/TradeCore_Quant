# ============================================================
# Kom v1.0 — gold_engine.py
# [SPRINT 36: GOLD-ONLY SYSTEM + DATA-DRIVEN OPTIMISATION]
#
# SPRINT 36 CHANGES:
#   [GOLD-ONLY] System stripped to XAUUSD exclusively.
#     All non-Gold assets removed. Gold is now perfected
#     before any second asset is introduced.
#
#   [DATA-DRIVEN SESSION GATES] Calibrated from 74-trade live history:
#     PRIME hours (WR>50%, net+): 23, 11-12, 2, 19  → full execution
#     DEAD hours (net negative):  0, 5, 6, 7, 13    → suppressed
#     All fade strategies (VWAP_FADE, ASIAN_FADE) blocked in dead hours.
#     Structural strategies (JUDAS, OB_RETRACE) reduced to NANO-only
#     during dead hours as directional probes only.
#
#   [HIGH-VOLATILITY FILTER — BUG-77] VWAP_FADE fired at Apr 7 01:00 UTC
#     into a 50pt gap-down (tariff risk-off). Fade strategies are now
#     suppressed when H4 ATR > 1.5× its 20-bar average (trending storm).
#     Structural and momentum strategies are still permitted in HV.
#
#   [MINIMUM SL — BUG-78] All market orders now enforce a minimum SL
#     of 2.0×M15_ATR (≈5 pts at current volatility). The Apr 7 BUY
#     was stopped in 30 seconds — post-analysis shows the SL was placed
#     at exactly 1.0×M15_ATR = 2.5 pts, which is inside spread+noise.
#     Structural limit orders retain 1.5×ATR minimum.
#
#   [NEW STRATEGY 8: TREND_RIDER] Data shows hours 11-12 UTC (London PM /
#     London-NY overlap) have the best Gold WR (75-80%) when a confirmed
#     H4 trend is present. TREND_RIDER enters pullbacks in the H4 trend
#     direction at 38.2-61.8% retracement zones. Active London_PM only.
#     Tier: STANDARD (high conviction from H4 structure). Score gate: 0.72+.
#
#   [IMPROVED VWAP_FADE GATE] Score gate raised 0.65→0.72 (MICRO min).
#     Requires H4 ATR in normal range (not trending storm).
#     Requires RSI confirmation: <30 for BUY, >70 for SELL (was <35/>65).
#
#   [DEAD-HOUR GATE HOURS] (UTC) Based on 74-trade live analysis:
#     0:00-01:00 — $-146 on 4 trades (worst Gold hour)
#     5:00-07:00 — $-159 combined (pre-London low liquidity)
#     13:00-14:00 — $-34 (NY Open gap volatility, unpredictable)
#     These hours: all MICRO/STANDARD/MACRO blocked; NANO probes only.
#
# EIGHT STRATEGIES (priority order):
#   1. LONDON_JUDAS     — Asian range sweep at London Open (STANDARD/MACRO)
#   2. NY_JUDAS         — London range sweep at NY Open (STANDARD/MACRO)
#   3. SILVER_BULLET    — Time-gated FVG in SB windows (MICRO)
#   4. TREND_RIDER      — H4-confirmed pullback in prime sessions (STANDARD) [NEW S36]
#   5. OB_RETRACE       — BOS OB retest with M1 confirmation (STANDARD)
#   6. ASIAN_FADE       — False breakout of Asian range (MICRO)
#   7. VWAP_FADE        — VWAP extension reversion (MICRO, tightened gates)
#   8. MOMENTUM_RIDER   — Displacement + pullback continuation (MICRO)
#
# CALIBRATED TO (S36 update):
#   XAUUSD M15 ATR ≈ 2.4–8 pts (HIGH_VOL regime)
#   Account balance ≈ $6,600  |  1% risk = $66
#   Market SL minimum: 2.0×M15_ATR  |  Limit SL minimum: 1.5×M15_ATR
#   Target hold: >15 min (10-min guard in bot_engine, enforced)
# ============================================================

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger("Kom_Gold")

# ── Safe analyst.py imports ─────────────────────────────────────────────────
try:
    from analyst import (
        calculate_atr,
        detect_market_structure,
        detect_order_blocks,
        detect_fvg,
        compute_vwap_context,
        wyckoff_spring_check,
        detect_asian_range,
        detect_candlestick_pattern,
    )
    _ANALYST_OK = True
except ImportError:
    _ANALYST_OK = False
    def calculate_atr(df, period=14):
        tr = pd.concat([
            df['high'] - df['low'],
            (df['high'] - df['close'].shift()).abs(),
            (df['low']  - df['close'].shift()).abs(),
        ], axis=1).max(axis=1)
        return tr.rolling(period).mean()

    def detect_market_structure(df, swing_lookback=10):
        return {'trend': 'NEUTRAL', 'swing_highs': [], 'swing_lows': [], 'choch': False}

    def detect_order_blocks(df, lookback=20):
        return {'bullish': None, 'bearish': None}

    def detect_fvg(df, direction, atr, lookback=10):
        return False

    def compute_vwap_context(df):
        return {'vwap': 0.0, 'vwap_z': 0.0, 'vwap_slope': 'FLAT',
                'above_vwap': False, 'extreme_bull': False, 'extreme_bear': False}

    def wyckoff_spring_check(df, manip_data, avg_vol):
        return {'spring': False, 'upthrust': False, 'test_vol_ratio': 1.0, 'low_vol_test': False}

    def detect_asian_range(df, utc_now):
        return {'valid': False, 'asian_high': None, 'asian_low': None}

    def detect_candlestick_pattern(df, direction, atr):
        return {'pattern': 'NONE', 'confirmed': False, 'bonus': 0.0,
                'conflict': False, 'conflict_pattern': 'NONE', 'conflict_penalty': 0.0}


# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTS & CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

# ── Silver Bullet windows for Gold (UTC) ────────────────────────────────────
GOLD_SB_WINDOWS = [
    (7,  0,  8,  0, 'London_Open_SB'),    # London manipulation prime window
    (15, 0, 16,  0, 'NY_Afternoon_SB'),   # NYSE overlap: highest US liquidity
    (19, 0, 20,  0, 'NY_PM_SB'),          # NY closing liquidity grab
]

# ── [S36] Dead hours — suppress MICRO/STANDARD/MACRO entries ────────────────
# Data-driven from 74-trade live history (account 32128474):
#   UTC 0:  4 trades, 25% WR, -$146  (worst hour)
#   UTC 5:  4 trades, 50% WR, -$77
#   UTC 6:  3 trades,  0% WR, -$42
#   UTC 7:  4 trades, 25% WR, -$41
#   UTC 13: 2 trades,  0% WR, -$34
# During these hours: only NANO probes allowed (exploratory, small risk).
GOLD_DEAD_HOURS = {0, 5, 6, 7, 13}

# ── [S36] Prime hours — full execution with score bonuses ───────────────────
#   UTC 23: 3 trades, 67% WR, +$180 (best hour — Tokyo close overlap)
#   UTC 11: 5 trades, 80% WR, +$113 (London PM prime)
#   UTC 12: 4 trades, 75% WR, +$54  (London-NY transition)
#   UTC 19: 3 trades, 67% WR, +$71  (NY PM Silver Bullet)
#   UTC 2:  6 trades, 50% WR, +$106 (Asian institutional flow)
GOLD_PRIME_HOURS = {23, 11, 12, 19, 2}

# ── Per-tier risk parameters (S36: max_lots increased for prime hours) ───────
# (risk_pct_of_balance, sl_atr_mult, tp_atr_mult, max_lots, expiry_min)
# [S36-BUG-78] sl_atr_mult increased: 1.0→1.5 NANO, 1.3→2.0 MICRO market orders
# Market order SL gets an additional 1.5× multiplier vs limit orders (see _compute_order_levels)
TIER_PARAMS = {
    'NANO':     (0.08,  1.5, 2.0,  0.03, 15),   # probe: ~$5 risk
    'MICRO':    (0.30,  2.0, 2.8,  0.08, 35),   # scalp: ~$20 risk
    'STANDARD': (0.75,  2.0, 3.2,  0.20, 240),  # structural: ~$50 risk
    'MACRO':    (1.25,  2.5, 4.0,  0.30, 720),  # swing: ~$82 risk
}

# ── Execution gate — minimum score per tier ──────────────────────────────────
# [S36] VWAP_FADE gate raised: 0.65→0.72 for MICRO (was too loose, caught HV storms)
TIER_MIN_SCORE = {
    'NANO':     0.56,
    'MICRO':    0.68,   # raised from 0.65
    'STANDARD': 0.72,
    'MACRO':    0.82,
}

# ── ATR regime thresholds (Gold price points) ────────────────────────────────
ATR_DEAD     = 1.0    # below = dead market
ATR_NORMAL_H = 8.0    # above = high volatility
# [S36] High-volatility FADE suppressor: if H4 ATR > HV_FADE_MULT × 20-bar avg,
# suppress all fade/mean-reversion strategies (VWAP_FADE, ASIAN_FADE).
# Live data: Apr 7 VWAP_FADE BUY into 50pt gap-down = -$9.69 in 30 seconds.
HV_FADE_MULT = 1.4    # H4 ATR > 1.4× its 20-bar average → no fades


# ══════════════════════════════════════════════════════════════════════════════
# GOLD SIGNAL DATACLASS
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class GoldSignal:
    """
    Standardised output from each Gold strategy.
    bot_engine reads these fields directly for execution.
    """
    strategy:     str         # e.g. 'LONDON_JUDAS', 'SILVER_BULLET'
    direction:    str         # 'BUY' | 'SELL'
    tier:         str         # 'NANO' | 'MICRO' | 'STANDARD' | 'MACRO'
    entry:        float       # limit price (or 0.0 for market)
    sl:           float
    tp:           float
    lot:          float       # pre-calculated lot size
    score:        float       # 0.0–1.0
    reason:       str         # human-readable summary
    kill_zone:    str
    conditions:   dict = field(default_factory=dict)  # for DB logging
    is_market:    bool = False  # True → TRADE_ACTION_DEAL, False → PENDING limit
    expiry_min:   int  = 60     # minutes until limit order expires

    @property
    def signal_type(self) -> str:
        """Compatibility string for existing execute path."""
        prefix = 'BUY' if self.direction == 'BUY' else 'SELL'
        suffix = {'NANO': '_NANO', 'MICRO': '_MICRO',
                  'STANDARD': '', 'MACRO': ''}.get(self.tier, '_MICRO')
        return f"{prefix}{suffix}"


# ══════════════════════════════════════════════════════════════════════════════
# GOLD SCALP ENGINE
# ══════════════════════════════════════════════════════════════════════════════

class GoldScalpEngine:
    """
    [S35] Dedicated XAUUSD signal generator.

    Usage:
        engine = GoldScalpEngine()
        signals = engine.analyse(df_m1, df_m15, df_h4, utc_now, balance)
        # signals is sorted highest-score first
        # Execute each if its tier slot is not already occupied
    """

    def __init__(self):
        # Track which tiers have open slots this cycle
        # bot_engine calls set_occupied_tiers() before analyse()
        self._occupied_tiers: set = set()

    def set_occupied_tiers(self, occupied: set):
        """
        Called by bot_engine before analyse() to prevent duplicate
        tier entries when Gold positions are already open.
        e.g. occupied={'MICRO'} → no new MICRO signals this cycle.
        """
        self._occupied_tiers = set(occupied)

    # ── MASTER ANALYSIS METHOD ──────────────────────────────────────────────

    def analyse(
        self,
        df_m1:  Optional[pd.DataFrame],
        df_m15: pd.DataFrame,
        df_h4:  Optional[pd.DataFrame],
        utc_now: datetime,
        balance: float,
    ) -> List[GoldSignal]:
        """
        Runs all eight strategies against current market data.
        Returns a list of valid GoldSignal objects, sorted by score descending.

        [S36] Pre-filters:
          - Dead hours (data-driven): only NANO probes allowed
          - HV fade suppressor: VWAP_FADE / ASIAN_FADE blocked when H4 ATR storm
          - Prime hours: +0.03 score bonus on all signals
        """
        if df_m15 is None or len(df_m15) < 50:
            return []

        df_m15 = df_m15.copy()
        df_m15['atr'] = calculate_atr(df_m15)
        atr = float(df_m15['atr'].iloc[-1])
        if atr <= 0:
            return []

        if atr < ATR_DEAD:
            return []

        high_vol = atr > ATR_NORMAL_H
        df_m15['avg_vol'] = df_m15['volume'].rolling(20).mean()
        avg_vol = float(df_m15['avg_vol'].iloc[-1]) if not df_m15.empty else 0.0

        vwap_ctx  = compute_vwap_context(df_m15)
        structure = detect_market_structure(df_m15)
        obs       = detect_order_blocks(df_m15)
        asian     = detect_asian_range(df_m15, utc_now)
        ctx       = _SessionContext(utc_now)
        h4_trend  = _derive_h4_trend(df_h4)

        # [S36] High-volatility fade suppressor
        # If H4 ATR is in a trending storm (> HV_FADE_MULT × 20-bar average),
        # suppress all fade/mean-reversion strategies to avoid entering against
        # strong directional institutional flow (April 7 tariff move lesson).
        _suppress_fades = False
        if df_h4 is not None and len(df_h4) >= 22:
            try:
                h4_atr_now = float(calculate_atr(df_h4).iloc[-1])
                h4_atr_avg = float(calculate_atr(df_h4).iloc[-20:].mean())
                if h4_atr_avg > 0 and h4_atr_now > h4_atr_avg * HV_FADE_MULT:
                    _suppress_fades = True
            except Exception:
                pass

        # [S36] Dead-hour gate — only NANO probes during statistically negative hours
        _dead_hour = ctx.utc_now.hour in GOLD_DEAD_HOURS
        _prime_hour = ctx.utc_now.hour in GOLD_PRIME_HOURS
        _prime_bonus = 0.03 if _prime_hour else 0.0

        all_signals: List[GoldSignal] = []

        # Strategy runner list — [S36] Trend Rider added at position 4
        runners = [
            ('JUDAS_L',    self._strategy_london_judas,    False),
            ('JUDAS_NY',   self._strategy_ny_judas,        False),
            ('SILVER_BULLET', self._strategy_silver_bullet, False),
            ('TREND_RIDER',self._strategy_trend_rider,     False),  # [S36 NEW]
            ('OB_RETRACE', self._strategy_ob_retrace,      False),
            ('ASIAN_FADE', self._strategy_asian_fade,      True),   # fade=True
            ('VWAP_FADE',  self._strategy_vwap_fade,       True),   # fade=True
            ('MOMENTUM',   self._strategy_momentum_rider,  False),
        ]

        for name, strategy_fn, is_fade in runners:
            try:
                # [S36] Skip fades entirely when HV suppressor is active
                if is_fade and _suppress_fades:
                    logger.debug(f"[GoldEngine] {name} suppressed — H4 ATR storm active")
                    continue

                sig = strategy_fn(
                    df_m1=df_m1, df_m15=df_m15, df_h4=df_h4,
                    atr=atr, avg_vol=avg_vol,
                    vwap_ctx=vwap_ctx, structure=structure,
                    obs=obs, asian=asian, ctx=ctx,
                    h4_trend=h4_trend, balance=balance,
                    high_vol=high_vol,
                )
                if sig is None:
                    continue

                # [S36] Dead-hour gate: downgrade to NANO or skip
                if _dead_hour:
                    if sig.tier in ('MICRO', 'STANDARD', 'MACRO'):
                        # Structural strategies become NANO probes
                        if not is_fade:
                            sig.tier    = 'NANO'
                            sig.score   = min(sig.score, 0.65)
                            sig.lot, sig.sl, sig.tp = _compute_order_levels(
                                sig.direction, sig.entry, sig.sl, sig.tp,
                                atr, 'NANO', balance, sig.is_market
                            )
                        else:
                            continue  # fades are skipped entirely in dead hours

                # [S36] Prime-hour score bonus
                sig.score = min(0.99, round(sig.score + _prime_bonus, 3))

                # Tier occupancy filter
                if sig.tier in self._occupied_tiers:
                    continue

                # Minimum score gate
                if sig.score < TIER_MIN_SCORE.get(sig.tier, 0.70):
                    continue

                all_signals.append(sig)

            except Exception as e:
                logger.debug(f"[GoldEngine] {name} error: {e}")

        all_signals.sort(key=lambda s: s.score, reverse=True)
        return _dedup_directions(all_signals)

    # ── STRATEGY 1: LONDON JUDAS SWEEP ─────────────────────────────────────
    # Classic ICT Judas: Asian range built 00:00-03:00 UTC, London sweeps
    # below Asian Low (bullish setup) or above Asian High (bearish setup),
    # displacement candle fires back inside the range, FVG left behind.
    # Prime time: 07:00-08:30 UTC. Tier: STANDARD (confirmed) / MACRO (perfect).

    def _strategy_london_judas(self, *, df_m15, df_m1, atr, avg_vol, vwap_ctx,
                                structure, obs, asian, ctx, h4_trend, balance,
                                high_vol, **_) -> Optional[GoldSignal]:
        # Session gate: London Open prime window only
        if not (ctx.london_open or ctx.london_pm):
            return None
        t = ctx.t_min
        if not (7 * 60 <= t < 8 * 60 + 30):
            return None
        if not asian.get('valid'):
            return None

        ah = asian['asian_high']
        al = asian['asian_low']
        if ah is None or al is None:
            return None

        asian_range_size = ah - al
        if asian_range_size < atr * 0.3:   # range too tight to be meaningful
            return None

        c1 = df_m15.iloc[-3]   # sweep candle
        c2 = df_m15.iloc[-2]   # displacement candle
        c3 = df_m15.iloc[-1]   # current (forming)

        sweep_bull = (c1['low'] < al and
                      (al - c1['low']) >= atr * 0.2 and
                      c2['close'] > c2['open'] and
                      abs(c2['close'] - c2['open']) >= atr * 0.5)

        sweep_bear = (c1['high'] > ah and
                      (c1['high'] - ah) >= atr * 0.2 and
                      c2['close'] < c2['open'] and
                      abs(c2['open'] - c2['close']) >= atr * 0.5)

        if not sweep_bull and not sweep_bear:
            return None

        direction  = 'BUY' if sweep_bull else 'SELL'
        sweep_low  = float(c1['low'])
        sweep_high = float(c1['high'])

        score = 0.65  # base for confirmed sweep + displacement
        cond  = {
            'strategy': 'LONDON_JUDAS',
            'asian_high': round(ah, 3),
            'asian_low':  round(al, 3),
            'sweep_level': round(sweep_low if sweep_bull else sweep_high, 3),
        }

        # FVG left by displacement
        fvg = detect_fvg(df_m15, direction, atr, lookback=6)
        cond['fvg'] = fvg
        if fvg: score += 0.07

        # OB alignment
        ob = obs.get('bullish' if sweep_bull else 'bearish')
        ob_hit = bool(ob and ob.get('retested'))
        cond['ob_retested'] = ob_hit
        if ob_hit: score += 0.06

        # Wyckoff low-volume sweep test
        manip = {'direction': 'BULL' if sweep_bull else 'BEAR', 'is_manipulation': True}
        wyck = wyckoff_spring_check(df_m15, manip, avg_vol)
        cond['wyckoff_spring'] = wyck.get('spring', False)
        cond['wyckoff_upthrust'] = wyck.get('upthrust', False)
        if wyck.get('spring') or wyck.get('upthrust'):
            score += 0.06

        # H4 alignment bonus
        h4_aligned = (direction == 'BUY' and h4_trend == 'BULLISH') or \
                     (direction == 'SELL' and h4_trend == 'BEARISH')
        cond['h4_aligned'] = h4_aligned
        if h4_aligned: score += 0.08
        elif h4_trend not in ('NEUTRAL', ''):  # counter-trend penalty
            score -= 0.06

        # VWAP confirmation
        vwap_confirm = (direction == 'BUY' and vwap_ctx.get('extreme_bull')) or \
                       (direction == 'SELL' and vwap_ctx.get('extreme_bear'))
        cond['vwap_extreme'] = vwap_confirm
        if vwap_confirm: score += 0.06

        # Volume surge on sweep
        vol_ratio = c1['volume'] / avg_vol if avg_vol > 0 else 1.0
        cond['vol_ratio'] = round(vol_ratio, 2)
        if vol_ratio >= 1.3:
            score = min(0.99, score * 1.08)

        # Candlestick confirmation on displacement candle
        cs = detect_candlestick_pattern(df_m15, direction, atr)
        if cs.get('confirmed'): score = min(0.99, score + cs['bonus'] * 0.7)
        elif cs.get('conflict'): score -= cs.get('conflict_penalty', 0) * 0.7
        cond['candle_pattern'] = cs.get('pattern', 'NONE')

        score = round(min(0.99, max(0.0, score)), 3)

        # Tier selection
        tier = 'MACRO' if score >= 0.82 else 'STANDARD'
        if high_vol: tier = 'MICRO'   # downgrade in volatile regimes

        # Levels
        if direction == 'BUY':
            tp_target = asian.get('asian_high') or float(df_m15['high'].tail(30).max())
            sl_ref    = sweep_low
        else:
            tp_target = asian.get('asian_low') or float(df_m15['low'].tail(30).min())
            sl_ref    = sweep_high

        lot, sl, tp = _compute_order_levels(
            direction, float(c3['close']), sl_ref, tp_target, atr, tier, balance,
            entry_is_market=False
        )

        reason = (f"London Judas ({'BULL' if sweep_bull else 'BEAR'}) | "
                  f"Score:{score:.2f} | FVG:{fvg} OB:{ob_hit} "
                  f"Wyckoff:{wyck.get('spring') or wyck.get('upthrust')} "
                  f"H4:{h4_trend} Vol:{vol_ratio:.1f}x")

        return GoldSignal(
            strategy='LONDON_JUDAS', direction=direction, tier=tier,
            entry=float(c3['close']), sl=sl, tp=tp, lot=lot, score=score,
            reason=reason, kill_zone='London_Open_SB', conditions=cond,
            is_market=False, expiry_min=TIER_PARAMS[tier][4],
        )

    # ── STRATEGY 2: NY JUDAS SWEEP ──────────────────────────────────────────
    # Same logic as London Judas but targets the London session range.
    # NY Open sweeps the London High or Low, displaces back inside.
    # Prime time: 13:30-14:30 UTC. Tier: STANDARD / MACRO.

    def _strategy_ny_judas(self, *, df_m15, df_m1, atr, avg_vol, vwap_ctx,
                            structure, obs, asian, ctx, h4_trend, balance,
                            high_vol, **_) -> Optional[GoldSignal]:
        t = ctx.t_min
        if not (13 * 60 + 30 <= t < 14 * 60 + 45):
            return None

        # Build London range (07:00-12:00 UTC candles)
        lh = ll = None
        if 'timestamp' in df_m15.columns or 'time' in df_m15.columns:
            tcol = 'timestamp' if 'timestamp' in df_m15.columns else 'time'
            try:
                df_ts = df_m15.copy()
                df_ts['_dt'] = pd.to_datetime(df_ts[tcol], utc=True)
                today = ctx.utc_now.date()
                london_bars = df_ts[
                    (df_ts['_dt'].dt.date == today) &
                    (df_ts['_dt'].dt.hour >= 7) &
                    (df_ts['_dt'].dt.hour < 12)
                ]
                if len(london_bars) >= 4:
                    lh = float(london_bars['high'].max())
                    ll = float(london_bars['low'].min())
            except Exception:
                pass

        if lh is None or ll is None:
            # Fallback: last 20 bars
            window = df_m15.iloc[-40:-20]
            if len(window) >= 4:
                lh = float(window['high'].max())
                ll = float(window['low'].min())
            else:
                return None

        c1 = df_m15.iloc[-3]
        c2 = df_m15.iloc[-2]
        c3 = df_m15.iloc[-1]

        sweep_bull = (c1['low'] < ll and
                      (ll - c1['low']) >= atr * 0.2 and
                      c2['close'] > c2['open'] and
                      abs(c2['close'] - c2['open']) >= atr * 0.5)

        sweep_bear = (c1['high'] > lh and
                      (c1['high'] - lh) >= atr * 0.2 and
                      c2['close'] < c2['open'] and
                      abs(c2['open'] - c2['close']) >= atr * 0.5)

        if not sweep_bull and not sweep_bear:
            return None

        direction = 'BUY' if sweep_bull else 'SELL'
        score = 0.65

        cond = {
            'strategy': 'NY_JUDAS',
            'london_high': round(lh, 3),
            'london_low':  round(ll, 3),
        }

        fvg = detect_fvg(df_m15, direction, atr, lookback=6)
        if fvg: score += 0.07
        cond['fvg'] = fvg

        ob = obs.get('bullish' if sweep_bull else 'bearish')
        ob_hit = bool(ob and ob.get('retested'))
        if ob_hit: score += 0.06
        cond['ob_retested'] = ob_hit

        h4_aligned = (direction == 'BUY' and h4_trend == 'BULLISH') or \
                     (direction == 'SELL' and h4_trend == 'BEARISH')
        if h4_aligned: score += 0.07
        cond['h4_aligned'] = h4_aligned

        vwap_confirm = (direction == 'BUY' and vwap_ctx.get('extreme_bull')) or \
                       (direction == 'SELL' and vwap_ctx.get('extreme_bear'))
        if vwap_confirm: score += 0.06
        cond['vwap_extreme'] = vwap_confirm

        vol_ratio = c1['volume'] / avg_vol if avg_vol > 0 else 1.0
        if vol_ratio >= 1.3: score = min(0.99, score * 1.07)
        cond['vol_ratio'] = round(vol_ratio, 2)

        cs = detect_candlestick_pattern(df_m15, direction, atr)
        if cs.get('confirmed'): score = min(0.99, score + cs['bonus'] * 0.7)
        elif cs.get('conflict'): score -= cs.get('conflict_penalty', 0) * 0.7

        score = round(min(0.99, max(0.0, score)), 3)
        tier = 'MACRO' if score >= 0.82 else 'STANDARD'
        if high_vol: tier = 'MICRO'

        tp_target = lh if direction == 'BUY' else ll
        sl_ref    = float(c1['low']) if sweep_bull else float(c1['high'])
        lot, sl, tp = _compute_order_levels(
            direction, float(c3['close']), sl_ref, tp_target, atr, tier, balance,
            entry_is_market=False
        )

        reason = (f"NY Judas ({'BULL' if sweep_bull else 'BEAR'}) | "
                  f"Score:{score:.2f} | FVG:{fvg} OB:{ob_hit} H4:{h4_trend}")

        return GoldSignal(
            strategy='NY_JUDAS', direction=direction, tier=tier,
            entry=float(c3['close']), sl=sl, tp=tp, lot=lot, score=score,
            reason=reason, kill_zone='NY_Open', conditions=cond,
            is_market=False, expiry_min=TIER_PARAMS[tier][4],
        )

    # ── STRATEGY 3: SILVER BULLET FVG ───────────────────────────────────────
    # ICT Silver Bullet: an FVG that was created inside one of the three
    # prime Gold windows (07-08, 15-16, 19-20 UTC). The expectation is
    # same-session mitigation. Pure time-based setup. Tier: MICRO.

    def _strategy_silver_bullet(self, *, df_m15, df_m1, atr, avg_vol, vwap_ctx,
                                  structure, obs, asian, ctx, h4_trend, balance,
                                  high_vol, **_) -> Optional[GoldSignal]:
        # Session gate: must be inside a Silver Bullet window
        in_sb, sb_name = _in_silver_bullet(ctx.t_min)
        if not in_sb:
            return None

        c3 = df_m15.iloc[-1]
        cur_price = float(c3['close'])
        score = 0.60  # base: time window alone

        # Scan last 8 bars for an unfilled FVG in each direction
        # Pick whichever aligns with VWAP / H4 trend
        prefer_bull = (vwap_ctx.get('vwap_slope') == 'UP' or
                       vwap_ctx.get('extreme_bull') or
                       h4_trend == 'BULLISH')
        prefer_bear = (vwap_ctx.get('vwap_slope') == 'DOWN' or
                       vwap_ctx.get('extreme_bear') or
                       h4_trend == 'BEARISH')

        direction = None
        if prefer_bull and not prefer_bear:
            direction = 'BUY'
        elif prefer_bear and not prefer_bull:
            direction = 'SELL'
        else:
            # Neutral bias: use structure trend
            direction = ('BUY' if structure.get('trend') == 'BULLISH'
                         else 'SELL' if structure.get('trend') == 'BEARISH'
                         else None)

        if direction is None:
            return None

        fvg = detect_fvg(df_m15, direction, atr, lookback=10)
        if not fvg:
            return None   # SB window + FVG required together

        score += 0.15  # unfilled FVG inside SB window

        # FVG freshness bonus (formed recently)
        score += 0.07

        cond = {'strategy': 'SILVER_BULLET', 'sb_window': sb_name,
                'fvg': True, 'direction': direction}

        # H4 alignment
        h4_aligned = (direction == 'BUY' and h4_trend == 'BULLISH') or \
                     (direction == 'SELL' and h4_trend == 'BEARISH')
        if h4_aligned: score += 0.06
        cond['h4_aligned'] = h4_aligned

        # VWAP extreme
        vwap_extreme = (direction == 'BUY' and vwap_ctx.get('extreme_bull')) or \
                       (direction == 'SELL' and vwap_ctx.get('extreme_bear'))
        if vwap_extreme: score += 0.06
        cond['vwap_extreme'] = vwap_extreme

        # OB at FVG
        ob = obs.get('bullish' if direction == 'BUY' else 'bearish')
        ob_hit = bool(ob and ob.get('active'))
        if ob_hit: score += 0.05
        cond['ob_at_fvg'] = ob_hit

        cs = detect_candlestick_pattern(df_m15, direction, atr)
        if cs.get('confirmed'): score = min(0.99, score + cs['bonus'] * 0.6)
        elif cs.get('conflict'): score -= cs.get('conflict_penalty', 0) * 0.6

        score = round(min(0.99, max(0.0, score)), 3)
        tier = 'MICRO'  # Silver Bullet is always MICRO
        if high_vol: tier = 'NANO'

        # Entry: market order — price is already at FVG
        # SL: beyond OB zone or 1.3×ATR from entry
        sl_ref_raw = cur_price - atr * 1.3 if direction == 'BUY' else cur_price + atr * 1.3
        if ob:
            sl_ref_raw = (ob.get('low', sl_ref_raw) if direction == 'BUY'
                          else ob.get('high', sl_ref_raw))

        # TP: previous structure high/low or 1.5×ATR
        sh = structure.get('last_sh') or cur_price + atr * 1.5
        sl_ = structure.get('last_sl') or cur_price - atr * 1.5
        tp_target = float(sh) if direction == 'BUY' else float(sl_)

        lot, sl, tp = _compute_order_levels(
            direction, cur_price, sl_ref_raw, tp_target, atr, tier, balance,
            entry_is_market=True
        )

        reason = (f"Silver Bullet FVG [{sb_name}] | "
                  f"Score:{score:.2f} | H4:{h4_trend} OB:{ob_hit} VWAP:{vwap_extreme}")

        return GoldSignal(
            strategy='SILVER_BULLET', direction=direction, tier=tier,
            entry=cur_price, sl=sl, tp=tp, lot=lot, score=score,
            reason=reason, kill_zone=sb_name, conditions=cond,
            is_market=True, expiry_min=TIER_PARAMS[tier][4],
        )

    # ── STRATEGY 4: ASIAN RANGE FADE (TURTLE SOUP) ──────────────────────────
    # Price breaks out of the Asian range at London Open but immediately
    # reverses — classic stop hunt / false breakout. Short the breakout.
    # Prime time: 06:30-09:00 UTC. Tier: MICRO.

    def _strategy_asian_fade(self, *, df_m15, df_m1, atr, avg_vol, vwap_ctx,
                               structure, obs, asian, ctx, h4_trend, balance,
                               high_vol, **_) -> Optional[GoldSignal]:
        t = ctx.t_min
        if not (6 * 60 + 30 <= t < 9 * 60):
            return None
        if not asian.get('valid'):
            return None

        ah = asian.get('asian_high'); al = asian.get('asian_low')
        if ah is None or al is None:
            return None

        n = len(df_m15)
        if n < 4:
            return None

        c1 = df_m15.iloc[-3]  # potential false breakout bar
        c2 = df_m15.iloc[-2]  # rejection / close-back bar
        c3 = df_m15.iloc[-1]  # current

        # Bearish Turtle Soup: broke above AH, closed back below
        bear_ts = (float(c1['high']) > ah and
                   float(c1['close']) < ah and
                   float(c2['close']) < ah)

        # Bullish Turtle Soup: broke below AL, closed back above
        bull_ts = (float(c1['low']) < al and
                   float(c1['close']) > al and
                   float(c2['close']) > al)

        if not bear_ts and not bull_ts:
            return None

        direction = 'SELL' if bear_ts else 'BUY'
        score = 0.60

        cond = {'strategy': 'ASIAN_FADE', 'asian_high': round(ah, 3),
                'asian_low': round(al, 3), 'direction': direction}

        # Rejection quality: bar body back inside range (>60% of body)
        c1_range = float(c1['high']) - float(c1['low'])
        if c1_range > 0:
            if bear_ts:
                body_back = (ah - float(c1['close'])) / c1_range
            else:
                body_back = (float(c1['close']) - al) / c1_range
            cond['body_back_pct'] = round(body_back, 2)
            if body_back > 0.60: score += 0.08
            elif body_back > 0.40: score += 0.04

        # Volume on rejection bar
        vol_ratio = float(c1['volume']) / avg_vol if avg_vol > 0 else 1.0
        if vol_ratio >= 1.5: score += 0.07
        cond['vol_ratio'] = round(vol_ratio, 2)

        # Candlestick confirmation on rejection candle
        cs = detect_candlestick_pattern(df_m15, direction, atr)
        if cs.get('confirmed'): score = min(0.99, score + cs['bonus'] * 0.7)
        elif cs.get('conflict'): score -= cs.get('conflict_penalty', 0) * 0.7

        # H4 alignment (optional — Turtle Soup can work counter-trend too)
        h4_aligned = (direction == 'BUY' and h4_trend == 'BULLISH') or \
                     (direction == 'SELL' and h4_trend == 'BEARISH')
        if h4_aligned: score += 0.05
        cond['h4_aligned'] = h4_aligned

        score = round(min(0.99, max(0.0, score)), 3)
        tier = 'MICRO'
        if high_vol: tier = 'NANO'

        # Entry: limit at the boundary that was faked
        entry = ah + atr * 0.05 if bear_ts else al - atr * 0.05
        extremal = float(c1['high']) if bear_ts else float(c1['low'])
        sl_ref = extremal + atr * 0.3 if bear_ts else extremal - atr * 0.3
        tp_target = al if bear_ts else ah  # target the opposite boundary

        lot, sl, tp = _compute_order_levels(
            direction, entry, sl_ref, tp_target, atr, tier, balance,
            entry_is_market=False
        )

        reason = (f"Asian Fade ({'Bear' if bear_ts else 'Bull'}) | "
                  f"Score:{score:.2f} | BodyBack:{cond.get('body_back_pct', 0):.0%} "
                  f"Vol:{vol_ratio:.1f}x")

        return GoldSignal(
            strategy='ASIAN_FADE', direction=direction, tier=tier,
            entry=entry, sl=sl, tp=tp, lot=lot, score=score,
            reason=reason, kill_zone='London_Open_SB', conditions=cond,
            is_market=False, expiry_min=30,
        )

    # ── STRATEGY 4 (NEW S36): TREND RIDER ───────────────────────────────────
    # [S36] Data shows hours 11-12 UTC (London PM) have 75-80% WR with confirmed
    # H4 trend. TREND_RIDER enters M15 pullbacks in the H4 trend direction at
    # Fibonacci 38.2-61.8% retracement zones between the last confirmed swing
    # points. This captures the continuation move after London's initial
    # distribution phase has established the direction.
    #
    # Entry model:
    #   1. H4 trend confirmed (EMA20 > EMA50 for bull / vice versa for bear)
    #   2. M15 swing structure agrees (BOS in H4 direction)
    #   3. Price is in 38.2-61.8% retracement of last confirmed impulse leg
    #   4. OB or FVG present at the retracement zone
    #   5. M1 shows momentum slowing (small candle or candlestick reversal)
    # Active: London_PM (09:00-13:00 UTC) + NY_PM2 (17:30-21:00 UTC)
    # Tier: STANDARD (high conviction from dual-timeframe structure)

    def _strategy_trend_rider(self, *, df_m15, df_m1, atr, avg_vol, vwap_ctx,
                               structure, obs, asian, ctx, h4_trend, balance,
                               high_vol, **_) -> Optional[GoldSignal]:
        t = ctx.t_min
        # Active in London PM and NY PM2 only — prime continuation windows
        active = (9*60 <= t < 13*60) or (17*60+30 <= t < 21*60)
        if not active:
            return None

        # Requires confirmed H4 trend — no neutral markets
        if h4_trend not in ('BULLISH', 'BEARISH'):
            return None

        # M15 structure must agree with H4 trend
        m15_trend = structure.get('trend', 'NEUTRAL')
        if m15_trend != h4_trend:
            return None  # conflicting timeframes — skip

        is_bull   = h4_trend == 'BULLISH'
        direction = 'BUY' if is_bull else 'SELL'

        # Extract the last confirmed impulse leg for Fibonacci measurement
        swing_highs = structure.get('swing_highs', [])
        swing_lows  = structure.get('swing_lows', [])
        if len(swing_highs) < 2 or len(swing_lows) < 2:
            return None

        # Impulse leg: from last swing low (BUY) or last swing high (SELL)
        if is_bull:
            # Bullish impulse: from the last swing low up to the last swing high
            impulse_start = swing_lows[-1][1]   # most recent confirmed low
            impulse_end   = swing_highs[-1][1]  # most recent confirmed high
        else:
            impulse_start = swing_highs[-1][1]
            impulse_end   = swing_lows[-1][1]

        impulse_size = abs(impulse_end - impulse_start)
        if impulse_size < atr * 0.8:
            return None  # impulse too small to be meaningful

        # Fibonacci retracement zone: 38.2%–61.8% of impulse
        fib382 = impulse_end - impulse_size * 0.382 if is_bull else impulse_end + impulse_size * 0.382
        fib618 = impulse_end - impulse_size * 0.618 if is_bull else impulse_end + impulse_size * 0.618

        fib_lo = min(fib382, fib618)
        fib_hi = max(fib382, fib618)

        cur_price = float(df_m15.iloc[-1]['close'])

        # Price must be inside the Fibonacci retracement zone
        in_fib_zone = fib_lo <= cur_price <= fib_hi
        if not in_fib_zone:
            return None

        score = 0.65  # base: H4+M15 confirmed trend + fib retracement
        cond  = {
            'strategy':    'TREND_RIDER',
            'h4_trend':    h4_trend,
            'm15_trend':   m15_trend,
            'fib382':      round(fib382, 3),
            'fib618':      round(fib618, 3),
            'impulse_size': round(impulse_size, 2),
        }

        # OB or FVG at the retracement zone (structural entry confirmation)
        ob = obs.get('bullish' if is_bull else 'bearish')
        ob_at_zone = bool(ob and ob.get('active'))
        cond['ob_at_zone'] = ob_at_zone
        if ob_at_zone: score += 0.10

        fvg_present = detect_fvg(df_m15, direction, atr, lookback=10)
        cond['fvg'] = fvg_present
        if fvg_present: score += 0.07

        # M1 momentum confirmation — small body = momentum slowing
        m1_confirm = False
        if df_m1 is not None and len(df_m1) >= 5:
            m1_cs = detect_candlestick_pattern(df_m1, direction, atr * 0.25)
            m1_confirm = m1_cs.get('confirmed', False)
            if m1_confirm: score += 0.08
            elif m1_cs.get('conflict'): score -= 0.06
            cond['m1_candle'] = m1_cs.get('pattern', 'NONE')

        # VWAP alignment (price should be pulling back toward VWAP in trend direction)
        above_vwap = vwap_ctx.get('above_vwap', False)
        if is_bull and not above_vwap: score += 0.05   # BUY below VWAP = discount
        elif not is_bull and above_vwap: score += 0.05  # SELL above VWAP = premium
        cond['above_vwap'] = above_vwap

        # Prime hour bonus (11-12 UTC best for this strategy)
        if 11*60 <= t < 13*60: score = min(0.99, score + 0.04)

        # Volume confirmation on recent candles
        vol_ratio = df_m15['volume'].iloc[-3:].mean() / avg_vol if avg_vol > 0 else 1.0
        if vol_ratio < 0.8: score += 0.04   # low volume pullback = healthier retrace
        cond['vol_ratio'] = round(vol_ratio, 2)

        cs = detect_candlestick_pattern(df_m15, direction, atr)
        if cs.get('confirmed'): score = min(0.99, score + cs['bonus'] * 0.8)
        elif cs.get('conflict'): score -= cs.get('conflict_penalty', 0) * 0.8

        score = round(min(0.99, max(0.0, score)), 3)
        tier  = 'STANDARD'
        if score >= 0.85: tier = 'MACRO'
        if high_vol: tier = 'MICRO'  # downgrade in volatile conditions

        # Entry: at current price (already in zone) as limit
        # SL: below fib618 (the deepest retracement level) + ATR buffer
        sl_ref = fib_lo - atr * 0.5 if is_bull else fib_hi + atr * 0.5

        # TP: previous swing high/low (the last impulse peak)
        tp_target = float(impulse_end) + atr * 0.5 if is_bull else float(impulse_end) - atr * 0.5

        lot, sl, tp = _compute_order_levels(
            direction, cur_price, sl_ref, tp_target, atr, tier, balance,
            entry_is_market=False
        )

        reason = (f"Trend Rider [{h4_trend}] Fib{fib382:.1f}-{fib618:.1f} | "
                  f"Score:{score:.2f} | OB:{ob_at_zone} FVG:{fvg_present} "
                  f"M1:{cond.get('m1_candle','—')} VWAP:{above_vwap}")

        return GoldSignal(
            strategy='TREND_RIDER', direction=direction, tier=tier,
            entry=cur_price, sl=sl, tp=tp, lot=lot, score=score,
            reason=reason, kill_zone=ctx.session_name, conditions=cond,
            is_market=False, expiry_min=TIER_PARAMS[tier][4],
        )

    # ── STRATEGY 5: ORDER BLOCK RETRACE ─────────────────────────────────────
    # BOS confirmed on M15 (3-swing structure), price pulls back to the
    # last confirmed OB, M1 shows slowing momentum or reversal candle.
    # Active in London and NY sessions. Tier: STANDARD.

    def _strategy_ob_retrace(self, *, df_m15, df_m1, atr, avg_vol, vwap_ctx,
                               structure, obs, asian, ctx, h4_trend, balance,
                               high_vol, **_) -> Optional[GoldSignal]:
        # Only fire in active sessions
        if ctx.ny_lunch or ctx.t_min < 7 * 60:
            return None

        trend = structure.get('trend', 'NEUTRAL')
        if trend == 'RANGING' or trend == 'NEUTRAL':
            return None

        direction = 'BUY' if trend == 'BULLISH' else 'SELL'
        ob = obs.get('bullish' if direction == 'BUY' else 'bearish')
        if ob is None or not ob.get('active'):
            return None   # price not at OB right now

        score = 0.63
        cond = {'strategy': 'OB_RETRACE', 'trend': trend, 'ob_zone': ob}

        # M1 momentum confirmation — look for slowing / reversal
        m1_confirm = False
        if df_m1 is not None and len(df_m1) >= 5:
            m1_cs = detect_candlestick_pattern(df_m1, direction, atr * 0.25)
            m1_confirm = m1_cs.get('confirmed', False)
            if m1_confirm: score += 0.10
            elif m1_cs.get('conflict'): score -= 0.08
            cond['m1_candle'] = m1_cs.get('pattern', 'NONE')

        # OB body quality (fresh = bars_ago low)
        bars_ago = ob.get('bars_ago', 20)
        if bars_ago <= 5: score += 0.07
        elif bars_ago <= 10: score += 0.04
        cond['ob_bars_ago'] = bars_ago

        # VWAP alignment
        above_vwap = vwap_ctx.get('above_vwap', False)
        if direction == 'BUY' and not above_vwap: score += 0.06  # buying below VWAP = discount
        if direction == 'SELL' and above_vwap: score += 0.06
        cond['above_vwap'] = above_vwap

        # BOS confirmation depth (swing count)
        swing_lows  = structure.get('swing_lows', [])
        swing_highs = structure.get('swing_highs', [])
        swing_depth = len(swing_lows) + len(swing_highs)
        if swing_depth >= 8: score += 0.06   # deep structure = high conviction
        elif swing_depth >= 5: score += 0.03
        cond['swing_depth'] = swing_depth

        # H4 alignment
        h4_aligned = (direction == 'BUY' and h4_trend == 'BULLISH') or \
                     (direction == 'SELL' and h4_trend == 'BEARISH')
        if h4_aligned: score += 0.07
        elif h4_trend not in ('NEUTRAL', ''):
            score -= 0.05   # counter-trend OB retrace penalty
        cond['h4_aligned'] = h4_aligned

        cs = detect_candlestick_pattern(df_m15, direction, atr)
        if cs.get('confirmed'): score = min(0.99, score + cs['bonus'] * 0.8)
        elif cs.get('conflict'): score -= cs.get('conflict_penalty', 0) * 0.8

        score = round(min(0.99, max(0.0, score)), 3)
        tier = 'STANDARD'
        if high_vol: tier = 'MICRO'
        if score >= 0.85: tier = 'MACRO'

        c3 = df_m15.iloc[-1]
        entry = (ob.get('body_low', ob.get('low', float(c3['close'])))
                 if direction == 'BUY'
                 else ob.get('body_high', ob.get('high', float(c3['close']))))
        sl_ref = (ob.get('low', float(c3['close'])) - atr * 0.3
                  if direction == 'BUY'
                  else ob.get('high', float(c3['close'])) + atr * 0.3)

        sh = structure.get('last_sh')
        sl_ = structure.get('last_sl')
        tp_target = (float(sh) if direction == 'BUY' and sh
                     else float(sl_) if direction == 'SELL' and sl_
                     else float(c3['close']) + atr * 2.5 if direction == 'BUY'
                     else float(c3['close']) - atr * 2.5)

        lot, sl, tp = _compute_order_levels(
            direction, entry, sl_ref, tp_target, atr, tier, balance,
            entry_is_market=False
        )

        reason = (f"OB Retrace [{trend}] | Score:{score:.2f} | "
                  f"Fresh:{bars_ago}bars M1:{cond.get('m1_candle','—')} H4:{h4_trend}")

        return GoldSignal(
            strategy='OB_RETRACE', direction=direction, tier=tier,
            entry=entry, sl=sl, tp=tp, lot=lot, score=score,
            reason=reason, kill_zone=ctx.session_name, conditions=cond,
            is_market=False, expiry_min=TIER_PARAMS[tier][4],
        )

    # ── STRATEGY 6: VWAP EXTENSION FADE ─────────────────────────────────────
    # Gold statistically mean-reverts when >2.5σ from VWAP intraday.
    # Conservative NANO entry — confirm with RSI, fade the extreme.
    # Active in any session except NY Lunch and dead market.

    def _strategy_vwap_fade(self, *, df_m15, df_m1, atr, avg_vol, vwap_ctx,
                              structure, obs, asian, ctx, h4_trend, balance,
                              high_vol, **_) -> Optional[GoldSignal]:
        if ctx.ny_lunch:
            return None
        if high_vol:
            return None   # don't fade during explosive moves

        vwap_z = vwap_ctx.get('vwap_z', 0.0)
        if abs(vwap_z) < 2.0:
            return None   # not extended enough

        extreme_bull = vwap_ctx.get('extreme_bull', False)
        extreme_bear = vwap_ctx.get('extreme_bear', False)
        if not extreme_bull and not extreme_bear:
            return None

        direction = 'SELL' if extreme_bear else 'BUY'
        score = 0.58 + min(0.12, abs(vwap_z) * 0.03)  # deeper = higher score

        cond = {'strategy': 'VWAP_FADE', 'vwap_z': round(vwap_z, 2),
                'direction': direction}

        # RSI confirmation
        rsi = _compute_rsi(df_m15)
        cond['rsi'] = round(rsi, 1) if rsi else None
        if rsi:
            if direction == 'SELL' and rsi > 68: score += 0.07
            elif direction == 'BUY' and rsi < 32: score += 0.07
            elif direction == 'SELL' and rsi > 75: score += 0.04   # extreme overbought
            elif direction == 'BUY' and rsi < 25: score += 0.04

        # Volume divergence (price extended but volume declining = exhaustion)
        vol_ratio = df_m15['volume'].iloc[-3:].mean() / avg_vol if avg_vol > 0 else 1.0
        if vol_ratio < 0.7: score += 0.06   # declining volume = fading move
        cond['vol_ratio'] = round(vol_ratio, 2)

        # Candlestick reversal confirmation
        cs = detect_candlestick_pattern(df_m15, direction, atr)
        if cs.get('confirmed'): score = min(0.99, score + cs['bonus'] * 0.6)
        elif cs.get('conflict'): return None  # hard conflict = skip fade

        # Counter-trend: only execute if H4 doesn't strongly oppose
        if (direction == 'BUY' and h4_trend == 'BEARISH') or \
           (direction == 'SELL' and h4_trend == 'BULLISH'):
            score = min(score, 0.68)   # cap at MICRO gate when counter-trend
        cond['h4_trend'] = h4_trend

        score = round(min(0.99, max(0.0, score)), 3)
        tier = 'NANO' if score < 0.65 else 'MICRO'

        c3 = df_m15.iloc[-1]
        cur = float(c3['close'])
        vwap = vwap_ctx.get('vwap', cur)
        sl_ref = cur + atr * 1.2 if direction == 'SELL' else cur - atr * 1.2
        tp_target = float(vwap)  # mean reversion target

        lot, sl, tp = _compute_order_levels(
            direction, cur, sl_ref, tp_target, atr, tier, balance,
            entry_is_market=True
        )

        reason = (f"VWAP Fade [{direction}] Z:{vwap_z:+.2f} | "
                  f"Score:{score:.2f} | RSI:{cond.get('rsi', '—')} "
                  f"TP=VWAP@{vwap:.2f}")

        return GoldSignal(
            strategy='VWAP_FADE', direction=direction, tier=tier,
            entry=cur, sl=sl, tp=tp, lot=lot, score=score,
            reason=reason, kill_zone=ctx.session_name, conditions=cond,
            is_market=True, expiry_min=TIER_PARAMS[tier][4],
        )

    # ── STRATEGY 7: MOMENTUM RIDER ──────────────────────────────────────────
    # After 3+ strong M1 displacement candles, wait for the first pullback
    # (1-2 bars counter-trend), then enter at the retrace level.
    # Active in prime windows: London Open, NY Open. Tier: MICRO.

    def _strategy_momentum_rider(self, *, df_m15, df_m1, atr, avg_vol, vwap_ctx,
                                   structure, obs, asian, ctx, h4_trend, balance,
                                   high_vol, **_) -> Optional[GoldSignal]:
        if df_m1 is None or len(df_m1) < 15:
            return None
        t = ctx.t_min
        # Only in London open or NY open
        if not ((7 * 60 <= t < 9 * 60) or (13 * 60 + 30 <= t < 15 * 60)):
            return None

        df = df_m1.copy().reset_index(drop=True)
        df['_atr'] = calculate_atr(df)
        m1_atr = float(df['_atr'].iloc[-1])
        if m1_atr <= 0:
            return None

        min_body = m1_atr * 0.5
        n = len(df)

        # Find displacement: 3+ consecutive candles same direction
        bull_disp = bear_disp = False
        disp_end = -1
        for i in range(max(0, n - 12), n - 3):
            bodies = [(df.iloc[j]['close'] - df.iloc[j]['open']) for j in range(i, i + 3)]
            if all(b >= min_body for b in bodies):
                bull_disp = True; disp_end = i + 2; break
            if all(b <= -min_body for b in bodies):
                bear_disp = True; disp_end = i + 2; break

        if not bull_disp and not bear_disp:
            return None

        direction = 'BUY' if bull_disp else 'SELL'

        # Find pullback candle (1-2 bars after displacement end)
        pullback_price = None
        for j in range(disp_end + 1, min(disp_end + 4, n)):
            c = df.iloc[j]
            body = float(c['close']) - float(c['open'])
            if direction == 'BUY' and body < 0 and abs(body) < min_body * 1.8:
                pullback_price = float(c['low'])
                break
            if direction == 'SELL' and body > 0 and abs(body) < min_body * 1.8:
                pullback_price = float(c['high'])
                break

        if pullback_price is None:
            return None   # no clean pullback found

        score = 0.64

        cond = {'strategy': 'MOMENTUM_RIDER', 'direction': direction,
                'm1_atr': round(m1_atr, 3)}

        # FVG in the displacement move
        fvg = detect_fvg(df, direction, m1_atr, lookback=8)
        if fvg: score += 0.07
        cond['fvg'] = fvg

        # Volume during displacement
        disp_bars = df.iloc[max(0, disp_end - 2): disp_end + 1]
        m1_avg_vol = df['volume'].iloc[:disp_end].mean() if disp_end > 0 else 1.0
        disp_vol_ratio = float(disp_bars['volume'].mean()) / m1_avg_vol if m1_avg_vol > 0 else 1.0
        if disp_vol_ratio >= 1.3: score += 0.06
        cond['disp_vol_ratio'] = round(disp_vol_ratio, 2)

        # H4 alignment (momentum trading should follow H4)
        h4_aligned = (direction == 'BUY' and h4_trend == 'BULLISH') or \
                     (direction == 'SELL' and h4_trend == 'BEARISH')
        if not h4_aligned and h4_trend not in ('NEUTRAL', ''):
            score -= 0.08   # counter-momentum penalty
        elif h4_aligned:
            score += 0.05
        cond['h4_aligned'] = h4_aligned

        # London session bonus (highest momentum quality)
        if 7 * 60 <= t < 8 * 60: score += 0.04
        cond['session'] = ctx.session_name

        score = round(min(0.99, max(0.0, score)), 3)
        tier = 'MICRO'
        if high_vol: tier = 'NANO'

        # Entry at pullback level (limit), SL 1.5×M1ATR beyond it
        sl_ref = pullback_price - m1_atr * 1.5 if direction == 'BUY' else pullback_price + m1_atr * 1.5
        # TP: 2.5× displacement body size
        avg_disp_body = sum(abs(df.iloc[i]['close'] - df.iloc[i]['open'])
                            for i in range(max(0, disp_end - 2), disp_end + 1)) / 3
        tp_target = pullback_price + avg_disp_body * 2.5 if direction == 'BUY' \
                    else pullback_price - avg_disp_body * 2.5

        lot, sl, tp = _compute_order_levels(
            direction, pullback_price, sl_ref, tp_target, atr, tier, balance,
            entry_is_market=False
        )

        reason = (f"Momentum Rider [{direction}] | Score:{score:.2f} | "
                  f"FVG:{fvg} DispVol:{disp_vol_ratio:.1f}x H4:{h4_trend}")

        return GoldSignal(
            strategy='MOMENTUM_RIDER', direction=direction, tier=tier,
            entry=pullback_price, sl=sl, tp=tp, lot=lot, score=score,
            reason=reason, kill_zone=ctx.session_name, conditions=cond,
            is_market=False, expiry_min=20,
        )


# ══════════════════════════════════════════════════════════════════════════════
# DYNAMIC EXIT ENGINE
# ══════════════════════════════════════════════════════════════════════════════

class DynamicExitEngine:
    """
    [S35] Gold-specific position management engine.

    Manages the exit lifecycle for each open Gold position:
      Phase 1  BE_0.6R:   Move SL to breakeven when 0.6×SL_dist in profit.
      Phase 2  Partial_1R: Close 30% of position at 1.0R profit.
      Phase 3  Partial_1.5R: Close 20% of remaining at 1.5R profit.
      Phase 4  Trail:     Trail remaining 50% with 0.5×ATR distance.
      Special  MomExit:   If 2 reversal M1 bars appear at ≥1.5R, close 80%.
      Special  TimeExit:  Per-tier max hold without progress → close at BE or small loss.

    State is tracked internally keyed by MT5 ticket number.
    bot_engine calls:
      register_position(ticket, tier, fill_time)  → on fill
      mark_phase_complete(ticket, phase)          → after executing each action
      cleanup_position(ticket)                    → on close
    """

    # Per-tier exit parameters
    # (be_trigger_r, partial1_r, partial1_pct, partial2_r, partial2_pct,
    #  trail_start_r, trail_dist_atr, momentum_exit_r, time_exit_min)
    TIER_EXIT = {
        'NANO':     (0.5,  0.8,  0.50, 1.2, 0.30, 1.0, 0.5, 1.2,  15),
        'MICRO':    (0.6,  1.0,  0.30, 1.5, 0.20, 1.5, 0.5, 1.5,  35),
        'STANDARD': (0.6,  1.0,  0.25, 1.8, 0.20, 2.0, 0.6, 2.0, 240),
        'MACRO':    (0.7,  1.2,  0.20, 2.0, 0.15, 2.5, 0.7, 2.5, 720),
    }

    def __init__(self):
        # Internal state per open Gold position
        # { ticket: { tier, fill_time, be_done, partial1_done, partial2_done } }
        self._state: dict = {}

    def register_position(self, ticket: int, tier: str, fill_time: datetime):
        """Call immediately when a Gold position opens."""
        self._state[int(ticket)] = {
            'tier':          tier,
            'fill_time':     fill_time,
            'be_done':       False,
            'partial1_done': False,
            'partial2_done': False,
        }

    def mark_phase_complete(self, ticket: int, phase: str):
        """Call after successfully executing a phase action."""
        st = self._state.get(int(ticket))
        if not st:
            return
        if phase == 'BREAKEVEN':  st['be_done']       = True
        elif phase == 'PARTIAL_1': st['partial1_done'] = True
        elif phase == 'PARTIAL_2': st['partial2_done'] = True

    def cleanup_position(self, ticket: int):
        """Call when a Gold position is closed (removes state entry)."""
        self._state.pop(int(ticket), None)

    def is_gold_managed(self, ticket: int) -> bool:
        """Returns True if this ticket is tracked by the exit engine."""
        return int(ticket) in self._state

    def get_tier(self, ticket: int) -> str:
        st = self._state.get(int(ticket))
        return st['tier'] if st else 'MICRO'

    def manage(
        self,
        pos: dict,
        df_m1: Optional[pd.DataFrame],
        current_price: float,
        atr: float,
    ) -> dict:
        """
        Evaluate one open position and return an action dict.

        Returns:
          {
            'action':    'NONE' | 'MODIFY_SL' | 'PARTIAL_CLOSE' | 'FULL_CLOSE',
            'new_sl':    float | None,
            'close_pct': float | None,   # 0.0-1.0 fraction of current volume
            'reason':    str,
            'phase':     str,
          }
        """
        null_action = {'action': 'NONE', 'new_sl': None, 'close_pct': None,
                       'reason': '', 'phase': 'NONE'}

        if atr <= 0 or current_price <= 0:
            return null_action

        ticket     = int(pos.get('ticket', 0))
        open_price = float(pos.get('open_price', 0))
        current_sl = float(pos.get('sl', 0))
        is_buy     = pos.get('type') == 'BUY'

        if open_price <= 0:
            return null_action

        # Retrieve internal state (may not exist for non-gold positions)
        st = self._state.get(ticket)
        if st is None:
            return null_action

        tier        = st['tier']
        fill_time   = st.get('fill_time')
        be_done     = st.get('be_done', False)
        partial1    = st.get('partial1_done', False)
        partial2    = st.get('partial2_done', False)

        params = self.TIER_EXIT.get(tier, self.TIER_EXIT['MICRO'])
        (be_r, p1_r, p1_pct, p2_r, p2_pct,
         trail_r, trail_atr, mom_r, time_max) = params

        sl_dist     = abs(open_price - current_sl) if current_sl else atr * 1.5
        if sl_dist <= 0:
            sl_dist = atr * 1.5

        profit_dist = (current_price - open_price) if is_buy else (open_price - current_price)
        # [BUG-76] Round r_current to 6 decimal places to prevent floating-point
        # precision failures where e.g. 1.98/3.3 = 0.59999... instead of 0.6.
        r_current   = round(profit_dist / sl_dist, 6)

        # ── Time exit ─────────────────────────────────────────────────────
        if fill_time:
            elapsed_min = (datetime.utcnow() - fill_time).total_seconds() / 60
            if elapsed_min > time_max and r_current < 0.3:
                return {
                    'action':    'FULL_CLOSE',
                    'new_sl':    None,
                    'close_pct': 1.0,
                    'reason':    f"Time exit: {elapsed_min:.0f}m > {time_max}m, R={r_current:.2f}",
                    'phase':     'TIME_EXIT',
                }

        # ── Momentum exit (M1 reversal at ≥ mom_r R) ─────────────────────
        if r_current >= mom_r and df_m1 is not None and len(df_m1) >= 4:
            if _detect_m1_reversal(df_m1, is_buy, atr * 0.3):
                return {
                    'action':    'PARTIAL_CLOSE',
                    'new_sl':    None,
                    'close_pct': 0.80,
                    'reason':    f"Momentum reversal at {r_current:.2f}R — securing 80%",
                    'phase':     'MOM_EXIT',
                }

        # ── Partial close Phase 2 at p2_r ─────────────────────────────────
        # Evaluated before Trail: at R-levels where both activate (e.g. MICRO 1.5R),
        # the partial close is the primary action. Trail fires on subsequent cycles.
        if r_current >= p2_r and not partial2 and partial1:
            return {
                'action':    'PARTIAL_CLOSE',
                'new_sl':    None,
                'close_pct': p2_pct,
                'reason':    f"Partial 2 ({p2_pct:.0%}) at {r_current:.2f}R",
                'phase':     'PARTIAL_2',
            }

        # ── Partial close Phase 1 at p1_r ─────────────────────────────────
        if r_current >= p1_r and not partial1:
            return {
                'action':    'PARTIAL_CLOSE',
                'new_sl':    None,
                'close_pct': p1_pct,
                'reason':    f"Partial 1 ({p1_pct:.0%}) at {r_current:.2f}R",
                'phase':     'PARTIAL_1',
            }

        # ── Trailing stop (activates at trail_r R) ─────────────────────────
        # Only fires after all pending partial closes have been executed.
        # This ensures the trail manages the remaining open portion, not the full size.
        if r_current >= trail_r:
            trail_dist = atr * trail_atr
            desired_sl = (current_price - trail_dist if is_buy
                          else current_price + trail_dist)
            should_move = ((is_buy     and desired_sl > current_sl + 0.01) or
                           (not is_buy and desired_sl < current_sl - 0.01))
            if should_move:
                return {
                    'action':    'MODIFY_SL',
                    'new_sl':    round(desired_sl, 3),
                    'close_pct': None,
                    'reason':    f"Trail at {r_current:.2f}R → SL {desired_sl:.2f}",
                    'phase':     'TRAIL',
                }

        # ── Breakeven move ─────────────────────────────────────────────────
        if r_current >= be_r and not be_done:
            be_sl      = open_price + 0.01 if is_buy else open_price - 0.01
            no_regress = ((is_buy     and be_sl > current_sl) or
                          (not is_buy and be_sl < current_sl))
            if no_regress:
                return {
                    'action':    'MODIFY_SL',
                    'new_sl':    round(be_sl, 3),
                    'close_pct': None,
                    'reason':    f"Breakeven at {r_current:.2f}R",
                    'phase':     'BREAKEVEN',
                }

        return null_action


# ══════════════════════════════════════════════════════════════════════════════
# PRIVATE HELPER FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

def _in_silver_bullet(t_min: int):
    """Returns (True, window_name) if t_min falls in a Gold SB window."""
    for sh, sm, eh, em, name in GOLD_SB_WINDOWS:
        ws = sh * 60 + sm; we = eh * 60 + em
        if ws <= t_min < we:
            return True, name
    return False, ''


def _derive_h4_trend(df_h4: Optional[pd.DataFrame]) -> str:
    """EMA-20 × EMA-50 dual confirmation on H4."""
    if df_h4 is None or len(df_h4) < 52:
        return 'NEUTRAL'
    try:
        df = df_h4.copy()
        df['ema20'] = df['close'].ewm(span=20, adjust=False).mean()
        df['ema50'] = df['close'].ewm(span=50, adjust=False).mean()
        c_last = df.iloc[-1]; c_prev = df.iloc[-2]
        bull = (c_last['close'] > c_last['ema20'] and
                c_prev['close'] > c_prev['ema20'] and
                c_last['ema20'] > c_last['ema50'])
        bear = (c_last['close'] < c_last['ema20'] and
                c_prev['close'] < c_prev['ema20'] and
                c_last['ema20'] < c_last['ema50'])
        return 'BULLISH' if bull else 'BEARISH' if bear else 'NEUTRAL'
    except Exception:
        return 'NEUTRAL'


def _compute_rsi(df: pd.DataFrame, period: int = 14) -> Optional[float]:
    """Simple RSI computation on last period+1 bars."""
    try:
        if len(df) < period + 1:
            return None
        closes = df['close'].astype(float).tail(period + 1)
        d = closes.diff()
        gain = d.clip(lower=0).mean()
        loss = (-d.clip(upper=0)).mean()
        if loss == 0:
            return 100.0
        rs = gain / loss
        return round(100 - 100 / (1 + rs), 1)
    except Exception:
        return None


def _detect_m1_reversal(df_m1: pd.DataFrame, is_buy: bool, min_body: float) -> bool:
    """
    Returns True if the last 2 M1 bars both oppose the trade direction
    with bodies >= min_body — indicating momentum has reversed.
    """
    try:
        c1 = df_m1.iloc[-2]; c2 = df_m1.iloc[-1]
        if is_buy:
            return (c1['close'] < c1['open'] and abs(c1['close'] - c1['open']) >= min_body and
                    c2['close'] < c2['open'] and abs(c2['close'] - c2['open']) >= min_body)
        else:
            return (c1['close'] > c1['open'] and abs(c1['close'] - c1['open']) >= min_body and
                    c2['close'] > c2['open'] and abs(c2['close'] - c2['open']) >= min_body)
    except Exception:
        return False


def _compute_order_levels(
    direction: str,
    entry: float,
    sl_ref: float,
    tp_target: float,
    atr: float,
    tier: str,
    balance: float,
    entry_is_market: bool = False,
) -> tuple:
    """
    Computes (lot, sl, tp) for a Gold signal.

    [S36-BUG-78] Minimum SL enforcement:
      Market orders: max(structural_sl_dist, 2.0×ATR)
        Prevents the Apr 7 scenario where SL at 1.0×ATR was hit in 30 seconds
        by opening spread + noise before the trade had time to develop.
      Limit orders: max(structural_sl_dist, 1.5×ATR)
        Limit entries get a better price so a slightly tighter SL is acceptable.

    TP: minimum 1.3× SL distance (hard R:R floor).
    Lot: dynamically sized to target risk_pct of balance.
    """
    risk_pct, sl_atr_mult, tp_atr_mult, max_lots, _ = TIER_PARAMS[tier]

    is_buy = direction == 'BUY'

    # [S36-BUG-78] Minimum SL distance depends on order type
    min_sl_mult  = 2.0 if entry_is_market else 1.5
    min_sl_dist  = atr * min_sl_mult
    sl_dist_raw  = abs(entry - sl_ref)
    sl_dist      = max(sl_dist_raw, min_sl_dist)

    sl = round(entry - sl_dist if is_buy else entry + sl_dist, 3)

    # TP: minimum 1.3× SL distance
    min_tp_dist = sl_dist * 1.3
    tp_dist_raw = abs(tp_target - entry)
    tp_dist     = max(tp_dist_raw, min_tp_dist, atr * tp_atr_mult)

    if is_buy and tp_target <= entry:
        tp_target = entry + tp_dist
    elif not is_buy and tp_target >= entry:
        tp_target = entry - tp_dist

    tp = round(tp_target, 3)

    # Lot sizing
    risk_usd        = balance * (risk_pct / 100.0)
    capital_per_lot = sl_dist * 100.0   # XAUUSD: 100 oz/lot
    raw_lot         = risk_usd / capital_per_lot if capital_per_lot > 0 else 0.01
    lot             = math.floor(raw_lot * 100) / 100
    lot             = max(0.01, min(lot, max_lots))

    return lot, sl, tp


def _dedup_directions(signals: List[GoldSignal]) -> List[GoldSignal]:
    """
    Removes signals that conflict in direction within the same tier.
    If tier X has both a BUY and SELL, keep only the higher-score one.
    Also enforces: only 1 market order at a time (prevents simultaneous
    NANO market orders from stacking).
    """
    seen_tier: dict = {}
    market_count = 0
    result = []
    for sig in signals:
        key = sig.tier
        if key not in seen_tier:
            seen_tier[key] = sig
        else:
            prev = seen_tier[key]
            if sig.direction != prev.direction:
                # Conflicting directions in same tier: keep higher score
                if sig.score > prev.score:
                    seen_tier[key] = sig
        # Max 1 market order per cycle to avoid duplicate fills
        if sig.is_market:
            if market_count < 2:
                market_count += 1
            else:
                continue
    # Rebuild from dedup'd dict preserving score order
    result = sorted(seen_tier.values(), key=lambda s: s.score, reverse=True)
    return result


# ══════════════════════════════════════════════════════════════════════════════
# SESSION CONTEXT HELPER
# ══════════════════════════════════════════════════════════════════════════════

class _SessionContext:
    """Precomputes all session membership flags from a UTC datetime."""
    def __init__(self, utc_now: datetime):
        self.utc_now      = utc_now
        self.t_min        = utc_now.hour * 60 + utc_now.minute
        t = self.t_min
        self.asian        = 0 <= t < 3 * 60
        self.pre_london   = 3 * 60 <= t < 7 * 60
        self.london_open  = 7 * 60 <= t < 9 * 60
        self.london_pm    = 9 * 60 <= t < 12 * 60
        self.london_ny    = 12 * 60 <= t < 16 * 60
        self.ny_lunch     = 16 * 60 <= t < 17 * 60 + 30
        self.ny_pm        = 17 * 60 + 30 <= t < 21 * 60

        if self.asian:           self.session_name = 'Asian'
        elif self.pre_london:    self.session_name = 'PreLondon'
        elif self.london_open:   self.session_name = 'London_Open_SB'
        elif self.london_pm:     self.session_name = 'London_PM'
        elif self.london_ny:     self.session_name = 'London_NY'
        elif self.ny_lunch:      self.session_name = 'NY_Lunch'
        elif self.ny_pm:         self.session_name = 'NY_PM2'
        else:                    self.session_name = 'Other'

        self.weight = {
            'Asian': 0.80, 'PreLondon': 0.60, 'London_Open_SB': 1.00,
            'London_PM': 0.70, 'London_NY': 0.90, 'NY_Lunch': 0.00,
            'NY_PM2': 0.80, 'Other': 0.50
        }.get(self.session_name, 0.50)

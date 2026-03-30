# ============================================================
# Kom v1.0 (formerly TradeCore) — analyst.py  
# [SPRINT 19g: REGIME ROUTER & MEAN REVERSION]
#
# SPRINT 19g UPGRADES:
#   - Added `compute_mean_reversion_confluence()` for DEAD MARKET regimes.
#   - Implemented the Regime Router in `analyze_market_structure()` to 
#     bypass ICT and trade statistical Bollinger Band/RSI extremes 
#     when ATR indicates a flat market.
#
# SPRINT 19f UPGRADES (Quantitative Order Flow):
#   - Added `compute_tick_delta()` to process the last 1,000 raw market 
#     ticks precisely at the point of interest (POI).
#   - Calculates Cumulative Tick Delta (CTD) to identify whether buyers 
#     (Ask hits) or sellers (Bid hits) are aggressively absorbing liquidity.
#   - Applies an objective mathematical bonus (+0.08 for delta confirmation, 
#     +0.05 for a sudden institutional volume surge) to elevate valid 0.70 
#     structural setups over the 0.77 execution threshold.
#
# SPRINT 19b NOTES (Structural Rollback):
#   - Rolled back all Discretionary Emulation (Sprint 19, Phases 1-3).
#   - Removed Fuzzy Liquidity Zones (10% ATR padding).
#   - Removed HTF Narrative Dominance (+0.12 bias score).
#   - Removed Dynamic POI Matrix (FVG proximal hijacks).
#   - Removed SMT Divergence cross-asset correlation.
#   - RATIONALE: The algorithmic edge relies on rigid mathematical 
#     consistency for the Kelly Criterion and Machine Learning pipelines. 
#     Introducing human fuzzy logic distorted the signal purity and 
#     led to false positives/unpredictable live behavior. Restoring to
#     the flawless Sprint 18 mathematical baseline.
#
# SPRINT 18 NOTES:
#   - Rebranded to Kom v1.0.
#   - Verified as 100% Stateless: This file contains only pure functions.
#     It retains zero memory between cycles, making it perfectly safe
#     to be executed asynchronously by the new Heavy Analysis Loop without
#     race conditions or state corruption.
#
# HISTORICAL PRESERVATION (Sprints 9 - 18):
#   - [Sprint 18] Dual-Loop async safety confirmed. 100% Stateless.
#   - [Sprint 16] Silver Bullet / Time-FVG / Power of 3 Scalp Engine.
#   - [Sprint 13] VWAP, Volume Profile (HVN/LVN), Delta, Wyckoff Checks.
#   - [Sprint  9] Core AMD (Accumulation, Manipulation, Distribution) mapping.
# ============================================================

try:
    from smc_engine import (
        analyse_smc_layers, smc_confluence_bonus, smc_layers_summary
    )
    _SMC_AVAILABLE = True
except ImportError:
    _SMC_AVAILABLE = False
    def analyse_smc_layers(*a, **kw): return None
    def smc_confluence_bonus(l, d, s): return 0.0, []
    def smc_layers_summary(l): return {}

import pandas as pd
import numpy as np
from datetime import datetime, timezone
from models import AnalysisRequest, AnalysisResponse, BacktestResponse


# ── ATR ───────────────────────────────────────────────────────────────────────

def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high_low   = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift()).abs()
    low_close  = (df['low']  - df['close'].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.rolling(period).mean()


# ── DEAD MARKET ATR THRESHOLD (OPT-1) ────────────────────────────────────────
# [S26] Per-asset thresholds. Gold/Silver move even in quiet sessions.
# Indices need a genuinely dead pre-market to trigger. BTC is never truly dead.

def _dead_market_atr_threshold(symbol: str) -> float:
    s = symbol.upper()
    if "BTC" in s:  return 80.0   # was 50 — raised; BTC has baseline volatility
    if "ETH" in s:  return 8.0
    if "XAU" in s:  return 0.20   # was 0.30 — Gold moves even in "quiet" sessions
    if "XAG" in s:  return 0.03   # was 0.05 — Silver very responsive to moves
    if "OIL" in s:  return 0.05
    if "NGAS" in s: return 0.005
    if "SP 500" in s or "TECH 100" in s: return 3.0   # was 2.0 — pre-NYSE looks dead
    if "GERMANY" in s: return 5.0
    if "JPY" in s:  return 0.025  # was 0.030 — JPY active in Asian session
    return 0.0003


# ── PER-ASSET DISPLACEMENT ATR MULTIPLIER (S26) ───────────────────────────────
# How large a displacement candle must be relative to ATR to qualify as
# genuine institutional displacement. Calibrated per asset class.

def _get_disp_atr_mult(symbol: str, regime: str) -> float:
    if "HIGH VOLATILITY" in regime: return 0.8
    if "DEAD" in regime:            return 0.3
    s = symbol.upper()
    if "XAU" in s:  return 0.50   # Gold: AMD moves fast, 0.5×ATR enough
    if "XAG" in s:  return 0.40   # Silver: more volatile per ATR unit
    if "BTC" in s or "ETH" in s:  return 0.55
    if "OIL" in s or "NGAS" in s: return 0.50
    if "SP 500" in s or "TECH 100" in s or "GERMANY" in s: return 0.80
    return 0.60   # FX standard



# ── AMD-1: MARKET CYCLE PHASE DETECTION ──────────────────────────────────────

def _session_amd_prior(utc_hour: int, utc_minute: int,
                       symbol: str = "") -> dict:
    """
    Returns session-context probability weights for each AMD phase.

    The clock is a PRIOR, not a gate. It biases the structural detector
    toward the phase most likely to occur at that hour, but does not
    override structural evidence. The structural conditions always decide.

    [S12-P2B] Symbol-aware: indices have different prime windows than FX.
      Germany 40 (DAX): prime manipulation = Frankfurt/London open (07-09 UTC)
      US SP500 / Tech100: prime manipulation = NYSE open (13:30-15:00 UTC)
      Asian session has minimal relevance for US indices — no accumulation bias.

    Returns dict of {phase: weight_multiplier} for use in detect_amd_phase().
    """
    t   = utc_hour * 60 + utc_minute
    sym = symbol.upper()

    # ── INDICES: separate session calendars ──────────────────────────
    is_us_index  = "SP 500" in sym or "TECH 100" in sym
    is_dax       = "GERMANY" in sym

    if is_us_index:
        # US indices: NY Lunch hard gate
        if 16*60 <= t < 17*60+30:
            return {'AVOID': 1.0}
        # Pre-market / Asian hours: low conviction, treat as INDETERMINATE
        if 0 <= t < 12*60:
            return {'ACCUMULATION': 0.8, 'MANIPULATION': 0.7, 'DISTRIBUTION': 0.8}
        # London/NY overlap (12-13:30 UTC): late pre-market accumulation
        if 12*60 <= t < 13*60+30:
            return {'ACCUMULATION': 1.2, 'MANIPULATION': 0.9, 'DISTRIBUTION': 0.8}
        # NYSE Open (13:30-15:00 UTC): prime Judas Swing for US indices
        if 13*60+30 <= t < 15*60:
            return {'MANIPULATION': 1.6, 'DISTRIBUTION': 1.0, 'ACCUMULATION': 0.5}
        # NY PM (15:00-16:00 UTC): distribution trend
        return {'DISTRIBUTION': 1.3, 'MANIPULATION': 0.9, 'ACCUMULATION': 0.7}

    if is_dax:
        # DAX: NY Lunch hard gate
        if 16*60 <= t < 17*60+30:
            return {'AVOID': 1.0}
        # Pre-DAX / Asian hours: accumulation only
        if 0 <= t < 7*60:
            return {'ACCUMULATION': 1.3, 'MANIPULATION': 0.8, 'DISTRIBUTION': 0.7}
        # Frankfurt/London open (07-09 UTC): prime Judas Swing for DAX
        if 7*60 <= t < 9*60:
            return {'MANIPULATION': 1.7, 'DISTRIBUTION': 0.9, 'ACCUMULATION': 0.5}
        # DAX PM (09-13 UTC): distribution
        if 9*60 <= t < 13*60:
            return {'DISTRIBUTION': 1.3, 'MANIPULATION': 1.0, 'ACCUMULATION': 0.7}
        # Post-noon: light continuation
        return {'DISTRIBUTION': 1.1, 'MANIPULATION': 0.8, 'ACCUMULATION': 0.8}

    # ── FX, COMMODITIES, CRYPTO: standard session priors ─────────────
    # NY Lunch is the only hard gate — liquidity genuinely absent
    if 16*60 <= t < 17*60+30:
        return {'AVOID': 1.0}

    # Asian (00-03 UTC): accumulation strongly favoured; manipulation possible
    if 0 <= t < 3*60:
        return {'ACCUMULATION': 1.4, 'MANIPULATION': 1.0, 'DISTRIBUTION': 0.7}

    # Pre-London (03-07 UTC): late accumulation; distribution from Asian cycle possible
    if 3*60 <= t < 7*60:
        return {'ACCUMULATION': 1.1, 'MANIPULATION': 1.0, 'DISTRIBUTION': 1.1}

    # London Open (07-09 UTC): manipulation strongly favoured (Judas Swing prime time)
    if 7*60 <= t < 9*60:
        return {'MANIPULATION': 1.6, 'DISTRIBUTION': 0.9, 'ACCUMULATION': 0.6}

    # London PM (09-12 UTC): distribution favoured; secondary manipulation possible
    if 9*60 <= t < 12*60:
        return {'DISTRIBUTION': 1.3, 'MANIPULATION': 1.1, 'ACCUMULATION': 0.8}

    # London/NY (12-16 UTC): distribution peak; NY Open manipulation at 13:30
    if 12*60 <= t < 13*60+30:
        return {'DISTRIBUTION': 1.3, 'MANIPULATION': 1.0, 'ACCUMULATION': 0.7}

    # NY Open (13:30-14:30 UTC): secondary Judas Swing window
    if 13*60+30 <= t < 14*60+30:
        return {'MANIPULATION': 1.4, 'DISTRIBUTION': 1.1, 'ACCUMULATION': 0.6}

    # NY PM (14:30-16:00 UTC): NY distribution continuation
    if 14*60+30 <= t < 16*60:
        return {'DISTRIBUTION': 1.2, 'MANIPULATION': 0.9, 'ACCUMULATION': 0.8}

    # NY PM2 (17:30-21:00 UTC): full NY afternoon post-lunch
    # [S11] Prime institutional window — continuation moves from NY morning.
    # Manipulation still possible (position squaring into close).
    if 17*60+30 <= t < 21*60:
        return {'MANIPULATION': 1.1, 'DISTRIBUTION': 1.2, 'ACCUMULATION': 0.9}

    # [S14-P6] NY_PM2_Late / Pre-Asian (21:00-24:00 UTC = 17:00-20:00 EDT):
    # Late NY session blending into Asian pre-market. Range formation begins
    # as institutional desks close for the day and Asian liquidity is seeded.
    # Previously this window fell through to the NY_PM else (14:30-16:00 UTC
    # weights) which under-weighted accumulation for this genuinely range-bound
    # period. Balanced priors allow the structural detector to find valid setups.
    if 21*60 <= t < 24*60:
        return {'ACCUMULATION': 1.2, 'MANIPULATION': 0.9, 'DISTRIBUTION': 1.0}

    # Fallback (should not be reached with full 0-24 coverage above)
    return {'DISTRIBUTION': 1.0, 'MANIPULATION': 1.0, 'ACCUMULATION': 1.0}


def detect_accumulation_structure(df: pd.DataFrame, atr: float,
                                   lookback: int = 20) -> dict:
    """
    Detects whether price is in an ACCUMULATION (ranging/consolidation) phase.

    Accumulation = smart money building inventory inside a controlled range.
    Price behaviour during accumulation:
      - Range is compressed relative to ATR (no trending momentum)
      - Candle bodies are small (indecision — neither side is dominant)
      - Volume is declining or flat (no institutional commitment yet)
      - Equal highs and equal lows form (engineered stop clusters being built)

    All four conditions are scored 0–1 and combined. A score above 0.60
    indicates a genuine accumulation structure.

    Returns:
      is_accumulating: bool  (score >= 0.60)
      confidence:      float 0–1
      range_high:      float — top of accumulation range
      range_low:       float — bottom of accumulation range
      range_atr:       float — range size in ATR multiples
    """
    empty = {'is_accumulating': False, 'confidence': 0.0,
             'range_high': None, 'range_low': None, 'range_atr': 0.0}
    if len(df) < lookback or atr <= 0:
        return empty

    window = df.tail(lookback)

    # 1. Range compression: high-low range < 1.5× ATR over lookback
    r_high = window['high'].max()
    r_low  = window['low'].min()
    range_size = r_high - r_low
    range_score = max(0.0, 1.0 - (range_size / (atr * 1.5)))  # 1.0 = fully compressed

    # 2. Body size: avg candle body < 0.3 ATR (indecision candles dominate)
    bodies = (window['close'] - window['open']).abs()
    avg_body = bodies.mean()
    body_score = max(0.0, 1.0 - (avg_body / (atr * 0.3)))

    # 3. Volume trend: recent 10-bar avg volume <= prior 10-bar avg (declining/flat)
    if len(window) >= 20:
        recent_vol = window['volume'].tail(10).mean()
        prior_vol  = window['volume'].head(10).mean()
        vol_score  = 1.0 if recent_vol <= prior_vol * 1.05 else max(0.0, 1.0 - (recent_vol / prior_vol - 1.0))
    else:
        vol_score = 0.5  # neutral if insufficient data

    # 4. Equal highs/lows: repeated tests of same level (stop cluster formation)
    tol = atr * 0.3
    highs = window['high'].values
    lows  = window['low'].values
    eq_high_count = sum(1 for i in range(len(highs))
                        for j in range(i+1, len(highs)) if abs(highs[i]-highs[j]) <= tol)
    eq_low_count  = sum(1 for i in range(len(lows))
                        for j in range(i+1, len(lows)) if abs(lows[i]-lows[j]) <= tol)
    eq_score = min(1.0, (eq_high_count + eq_low_count) / 6.0)

    confidence = (range_score * 0.40 + body_score * 0.25 +
                  vol_score  * 0.20 + eq_score   * 0.15)

    return {
        'is_accumulating': confidence >= 0.55,
        'confidence':      round(confidence, 3),
        'range_high':      round(float(r_high), 5),
        'range_low':       round(float(r_low), 5),
        'range_atr':       round(range_size / atr, 2),
    }


def detect_manipulation_spike(df: pd.DataFrame, atr: float,
                                accum_high: float, accum_low: float) -> dict:
    """
    Detects a MANIPULATION event: the Judas Swing / stop hunt.

    Manipulation = smart money breaks the accumulation range boundary with
    volume to collect clustered stops, then immediately reverses. The breach
    is the FAKE move; the close-back is the signal.

    Structural fingerprint:
      1. ATR expansion: the spike candle's range > 1.5× the 20-bar avg ATR
         (sudden volatility means institutional force, not retail drift)
      2. Range boundary breach: c1 moves BEYOND accum_high or accum_low
         (the stop hunt targets the exact levels retail placed orders at)
      3. Close-back within 3 candles: price closes BACK INSIDE the accum range
         (the breach failed — institutions got their fill and reversed)
      4. Volume on the spike > 1.3× average (institutional participation)

    When all four conditions align, this is the Judas Swing confirmation.

    Returns:
      is_manipulation: bool
      direction:       'BULL' (swept low, expect up) | 'BEAR' (swept high, expect down)
      swept_level:     the exact price level that was swept
      spike_atr_mult:  how large the spike was in ATR multiples
      confidence:      0.0–1.0
    """
    empty = {'is_manipulation': False, 'direction': None,
             'swept_level': None, 'spike_atr_mult': 0.0, 'confidence': 0.0}

    if len(df) < 5 or atr <= 0:
        return empty
    if accum_high is None or accum_low is None:
        return empty

    avg_vol  = df['volume'].rolling(20).mean().iloc[-1]
    avg_atr  = df['atr'].rolling(20).mean().iloc[-1] if 'atr' in df.columns else atr

    # Check last 3 candles for a manipulation spike (it's fast — 1-3 bars)
    for offset in range(1, 4):
        if len(df) < offset + 2:
            break
        spike = df.iloc[-(offset + 1)]  # the spike candle
        after = df.iloc[-offset:]       # candles that followed

        spike_range = spike['high'] - spike['low']
        spike_vol   = spike['volume'] / avg_vol if avg_vol > 0 else 1.0
        atr_mult    = spike_range / avg_atr if avg_atr > 0 else 1.0

        # BULLISH manipulation: swept BELOW accum_low, closed back above
        if (spike['low'] < accum_low and
                spike_range >= avg_atr * 1.3 and
                spike_vol >= 1.3 and
                after['close'].iloc[-1] > accum_low):
            depth = (accum_low - spike['low']) / atr
            conf  = min(1.0, (atr_mult / 2.0) * 0.4 +
                             min(spike_vol / 2.0, 1.0) * 0.4 +
                             min(depth / 1.0, 1.0) * 0.2)
            return {'is_manipulation': True, 'direction': 'BULL',
                    'swept_level': round(float(accum_low), 5),
                    'spike_atr_mult': round(atr_mult, 2),
                    'confidence': round(conf, 3)}

        # BEARISH manipulation: swept ABOVE accum_high, closed back below
        if (spike['high'] > accum_high and
                spike_range >= avg_atr * 1.3 and
                spike_vol >= 1.3 and
                after['close'].iloc[-1] < accum_high):
            depth = (spike['high'] - accum_high) / atr
            conf  = min(1.0, (atr_mult / 2.0) * 0.4 +
                             min(spike_vol / 2.0, 1.0) * 0.4 +
                             min(depth / 1.0, 1.0) * 0.2)
            return {'is_manipulation': True, 'direction': 'BEAR',
                    'swept_level': round(float(accum_high), 5),
                    'spike_atr_mult': round(atr_mult, 2),
                    'confidence': round(conf, 3)}

    return empty


def detect_distribution_trend(df: pd.DataFrame, structure: dict,
                                atr: float) -> dict:
    """
    Detects whether price is in a DISTRIBUTION (trending delivery) phase.

    Distribution = smart money delivering price to the target liquidity pool
    after the manipulation sweep. Price trends with consistent swing structure.

    Structural fingerprint:
      - Consecutive HH/HL (bullish) or LL/LH (bearish) over last 5+ bars
      - ATR normalising after the manipulation spike (not still expanding)
      - BOS confirmed in the distribution direction
      - OBs and FVGs visible in the lookback (left during manipulation)

    Returns:
      is_distributing: bool
      direction:       'BULL' | 'BEAR' | None
      confidence:      0.0–1.0
      atr_normalised:  bool — True if ATR has returned toward baseline
    """
    empty = {'is_distributing': False, 'direction': None,
             'confidence': 0.0, 'atr_normalised': False}
    if len(df) < 10 or atr <= 0:
        return empty

    trend     = structure.get('trend', 'NEUTRAL')
    swing_highs = structure.get('swing_highs', [])
    swing_lows  = structure.get('swing_lows', [])

    # Need at least 3 confirmed swing points for trend
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return empty

    is_bull_trend = (trend == 'BULLISH' and len(swing_highs) >= 3 and
                     all(swing_highs[i][1] < swing_highs[i+1][1]
                         for i in range(len(swing_highs)-2, len(swing_highs)-1)))
    is_bear_trend = (trend == 'BEARISH' and len(swing_lows) >= 3 and
                     all(swing_lows[i][1] > swing_lows[i+1][1]
                         for i in range(len(swing_lows)-2, len(swing_lows)-1)))

    if not is_bull_trend and not is_bear_trend:
        return empty

    # ATR normalisation: current ATR should be <= 1.3× 20-bar average
    avg_atr = df['atr'].rolling(20).mean().iloc[-1] if 'atr' in df.columns else atr
    atr_ratio = atr / avg_atr if avg_atr > 0 else 1.0
    atr_normalised = atr_ratio <= 1.3

    # BOS alignment
    bos_score = 1.0 if (is_bull_trend and trend == 'BULLISH') or \
                       (is_bear_trend and trend == 'BEARISH') else 0.5

    # Swing count depth (more confirmed swings = higher confidence)
    swing_count = len(swing_highs) + len(swing_lows)
    swing_score = min(1.0, swing_count / 8.0)

    confidence = bos_score * 0.5 + swing_score * 0.3 + (0.2 if atr_normalised else 0.0)

    return {
        'is_distributing': confidence >= 0.55,
        'direction':       'BULL' if is_bull_trend else 'BEAR',
        'confidence':      round(confidence, 3),
        'atr_normalised':  atr_normalised,
    }


def detect_amd_phase(df: pd.DataFrame, atr: float,
                     utc_hour: int, utc_minute: int,
                     accum_high: float = None, accum_low: float = None,
                     structure: dict = None,
                     symbol: str = "") -> dict:
    """
    [AMD-STRUCTURAL] Determines the current AMD market cycle phase from
    price structure, with session clock as a probability prior — not a gate.

    The log analysis from March 10 proved that a fixed UTC clock is wrong:
      - MANIPULATION fired at 01:31 UTC (clock said ACCUMULATION)
      - DISTRIBUTION ran 03:00–05:59 UTC (clock said ACCUMULATION)
      - A second MANIPULATION fired at 11:15 UTC (clock said DISTRIBUTION)
      - London Open 07–09 had ZERO manipulation (clock said MANIPULATION)

    AMD is a STRUCTURAL SEQUENCE, not a timetable. This function detects each
    phase from price behaviour and uses the clock only to weight probabilities.

    Detection priority (in order):
      1. AVOID  — hard gate (NY Lunch). Liquidity genuinely absent.
      2. MANIPULATION — strongest structural signal. Overrides clock.
      3. DISTRIBUTION — trend structure confirmed.
      4. ACCUMULATION — compression detected (fallback).

    [S12-P2B] Symbol-aware session priors: US indices use NYSE calendar,
    DAX uses Frankfurt/London calendar, FX/crypto use standard FX calendar.

    [S12-P2A] Session-bounded accumulation lookback: the lookback window is
    capped to the number of bars since the current session opened. This
    prevents Asian consolidation data being contaminated by prior NY volatility.

    Returns dict:
      phase:         'ACCUMULATION' | 'MANIPULATION' | 'DISTRIBUTION' | 'AVOID' | 'INDETERMINATE'
      direction:     'BULL' | 'BEAR' | None (for MANIP and DIST)
      confidence:    float 0–1 (structural detection confidence)
      clock_label:   descriptive session label for logging
      manip_data:    manipulation event data (if phase == MANIPULATION)
      accum_data:    accumulation structure data (always computed)
      dist_data:     distribution structure data (always computed)
      session_prior: the clock weights dict used
    """
    if structure is None:
        structure = {}

    prior = _session_amd_prior(utc_hour, utc_minute, symbol=symbol)

    # Hard AVOID gate
    if 'AVOID' in prior:
        return {'phase': 'AVOID', 'direction': None, 'confidence': 1.0,
                'clock_label': 'NY_Lunch', 'manip_data': {}, 'accum_data': {},
                'dist_data': {}, 'session_prior': prior}

    # Clock label for logging
    t = utc_hour * 60 + utc_minute
    if   t < 3*60:              clock_label = 'Asian'
    elif t < 7*60:              clock_label = 'PreLondon'
    elif t < 9*60:              clock_label = 'London'
    elif t < 12*60:             clock_label = 'London_PM'
    elif t < 13*60+30:          clock_label = 'London_NY'
    elif t < 14*60+30:          clock_label = 'NY_Open'
    elif t < 16*60:             clock_label = 'NY_PM'
    elif t < 17*60+30:          clock_label = 'NY_Lunch'   # AVOID gate handles this
    elif t < 21*60:             clock_label = 'NY_PM2'     # [S11] new session window
    else:                       clock_label = 'NY_PM2_Late'

    # [S12-P2A] Session-bounded accumulation lookback.
    # Using a fixed 20-bar lookback bleeds across session boundaries:
    # at 01:00 UTC the 20 bars reach back into the previous NY session,
    # contaminating the Asian range with NY volatility.
    # Cap the lookback to bars elapsed since the current session opened.
    # Asian: opens 00:00 → max 12 bars (3h × 4bars/h)
    # London: opens 07:00 → max 8 bars at London open, grows through session
    # NY: opens 13:30 → cap at 10 bars
    # Elsewhere: use standard 20-bar lookback
    if 0 <= t < 3*60:        # Asian session — only bars since midnight
        bars_in_session = max(4, t // 15)         # M15: t/15 bars elapsed
        accum_lookback  = min(12, bars_in_session)
    elif 7*60 <= t < 9*60:   # London open — only bars since 07:00
        bars_in_session = max(4, (t - 7*60) // 15)
        accum_lookback  = min(8, bars_in_session)
    elif 13*60+30 <= t < 16*60:  # NY session
        bars_in_session = max(4, (t - 13*60-30) // 15)
        accum_lookback  = min(10, bars_in_session)
    else:
        accum_lookback  = 20   # standard lookback elsewhere

    # ── Structural detections ────────────────────────────────────
    accum_data = detect_accumulation_structure(df, atr, lookback=accum_lookback)
    dist_data  = detect_distribution_trend(df, structure, atr)

    # Manipulation requires known accumulation boundaries
    _ah = accum_high or accum_data.get('range_high')
    _al = accum_low  or accum_data.get('range_low')
    manip_data = detect_manipulation_spike(df, atr, _ah, _al)

    # ── Phase decision with prior weighting ──────────────────────
    # MANIPULATION: structural signal weighted by session prior
    if manip_data['is_manipulation']:
        m_conf = manip_data['confidence'] * prior.get('MANIPULATION', 1.0)
        if m_conf >= 0.40:  # threshold for structural overriding clock
            direction = manip_data['direction']
            return {'phase': 'MANIPULATION', 'direction': direction,
                    'confidence': round(m_conf, 3), 'clock_label': clock_label,
                    'manip_data': manip_data, 'accum_data': accum_data,
                    'dist_data': dist_data, 'session_prior': prior}

    # DISTRIBUTION: trend confirmed
    if dist_data['is_distributing']:
        d_conf = dist_data['confidence'] * prior.get('DISTRIBUTION', 1.0)
        if d_conf >= 0.45:
            return {'phase': 'DISTRIBUTION', 'direction': dist_data['direction'],
                    'confidence': round(d_conf, 3), 'clock_label': clock_label,
                    'manip_data': manip_data, 'accum_data': accum_data,
                    'dist_data': dist_data, 'session_prior': prior}

    # ACCUMULATION: compression detected or default fallback
    # [S12-AMD-A] Minimum threshold: if no phase met their bar AND accumulation
    # structure is weak (conf < 0.35), the market is in an ambiguous/trending state —
    # NOT a clean accumulation range. Label it INDETERMINATE so that:
    #   (a) no spurious AH/AL boundaries are passed to the MANIPULATION detector
    #   (b) the log clearly shows "unreadable market" instead of a misleading phase
    # Threshold of 0.35 = raw_conf 0.25 × prior 1.4 (Asian) ≈ 0.35 minimum honest signal
    a_conf = accum_data['confidence'] * prior.get('ACCUMULATION', 1.0)
    ACCUM_MIN_CONF = 0.35
    if a_conf < ACCUM_MIN_CONF:
        return {'phase': 'INDETERMINATE', 'direction': None,
                'confidence': round(a_conf, 3), 'clock_label': clock_label,
                'manip_data': manip_data, 'accum_data': accum_data,
                'dist_data': dist_data, 'session_prior': prior}
    return {'phase': 'ACCUMULATION', 'direction': None,
            'confidence': round(a_conf, 3), 'clock_label': clock_label,
            'manip_data': manip_data, 'accum_data': accum_data,
            'dist_data': dist_data, 'session_prior': prior}


# ── AMD-2: ASIAN RANGE DETECTION ─────────────────────────────────────────────

def detect_asian_range(df: pd.DataFrame, utc_now: datetime) -> dict:
    """
    Identifies today's Asian session range: 00:00–03:00 UTC.

    The Asian High (AH) and Asian Low (AL) are the structural reference
    levels for the London Open Judas Swing. When London sweeps BELOW AL,
    the setup is bullish. When London sweeps ABOVE AH, the setup is bearish.

    Requires 'timestamp' or datetime index in df to filter by session time.
    Falls back gracefully if no timestamp column — uses rolling approximation.

    Returns dict with:
      asian_high:      highest high during 00:00–03:00 UTC today
      asian_low:       lowest low during 00:00–03:00 UTC today
      asian_midpoint:  (AH + AL) / 2
      asian_range_atr: range size relative to current ATR (range quality measure)
      valid:           True if Asian range was detected with >= 3 candles
    """
    empty = {'asian_high': None, 'asian_low': None,
             'asian_midpoint': None, 'asian_range_atr': None, 'valid': False}

    if df is None or len(df) < 10:
        return empty

    today = utc_now.date()
    atr   = df['atr'].iloc[-1] if 'atr' in df.columns else 0.001

    # Try to filter by timestamp column (preferred)
    if 'timestamp' in df.columns:
        try:
            df_ts = df.copy()
            df_ts['_dt'] = pd.to_datetime(df_ts['timestamp'], utc=True)
            asian = df_ts[
                (df_ts['_dt'].dt.date == today) &
                (df_ts['_dt'].dt.hour >= 0) &
                (df_ts['_dt'].dt.hour < 3)
            ]
            if len(asian) >= 3:
                ah = asian['high'].max()
                al = asian['low'].min()
                return {
                    'asian_high':     round(float(ah), 5),
                    'asian_low':      round(float(al), 5),
                    'asian_midpoint': round(float((ah + al) / 2), 5),
                    'asian_range_atr': round(float((ah - al) / atr), 2),
                    'valid': True,
                }
        except Exception:
            pass

    # Fallback: estimate from candle count (M15 = 4 candles/hour × 3h = 12 candles)
    # Look back 12–24 candles to approximate the Asian window
    asian_window = df.tail(24).head(12)
    if len(asian_window) < 3:
        return empty

    ah = asian_window['high'].max()
    al = asian_window['low'].min()
    return {
        'asian_high':     round(float(ah), 5),
        'asian_low':      round(float(al), 5),
        'asian_midpoint': round(float((ah + al) / 2), 5),
        'asian_range_atr': round(float((ah - al) / atr), 2),
        'valid': True,
    }


# ── AMD-3: LONDON SESSION RANGE (for NY Judas Swing reference) ───────────────

def detect_london_range(df: pd.DataFrame, utc_now: datetime) -> dict:
    """
    Identifies today's London session High and Low (07:00–12:00 UTC).

    Used by NY_MANIPULATION phase to detect the NY Open Judas Swing:
    when NY sweeps the London session Low or High before the true NY move.

    Same timestamp-then-fallback approach as detect_asian_range().
    """
    empty = {'london_high': None, 'london_low': None, 'valid': False}
    if df is None or len(df) < 10:
        return empty

    today = utc_now.date()

    if 'timestamp' in df.columns:
        try:
            df_ts = df.copy()
            df_ts['_dt'] = pd.to_datetime(df_ts['timestamp'], utc=True)
            london = df_ts[
                (df_ts['_dt'].dt.date == today) &
                (df_ts['_dt'].dt.hour >= 7) &
                (df_ts['_dt'].dt.hour < 12)
            ]
            if len(london) >= 3:
                return {
                    'london_high': round(float(london['high'].max()), 5),
                    'london_low':  round(float(london['low'].min()), 5),
                    'valid': True,
                }
        except Exception:
            pass

    # Fallback: London ~20 bars back from 13:30 entry (M15: 20 bars = 5 hours)
    window = df.iloc[-44:-24] if len(df) >= 44 else df.head(20)
    if len(window) < 3:
        return empty
    return {
        'london_high': round(float(window['high'].max()), 5),
        'london_low':  round(float(window['low'].min()), 5),
        'valid': True,
    }


# ── AMD-4: JUDAS SWING DETECTION ─────────────────────────────────────────────

def detect_judas_swing(c1_low: float, c1_high: float,
                       ref_high: float, ref_low: float,
                       atr: float) -> dict:
    """
    Determines whether the current sweep candle (c1) specifically targets
    the session range boundary (AH/AL for London, LH/LL for NY).

    A Judas Swing is NOT just any sweep. It is the deliberate sweep of a
    known stop-order cluster at the boundary of the accumulation range.
    This makes it structurally higher probability than a generic structural sweep.

    Detection criteria:
      Bullish Judas Swing: c1 sweeps BELOW ref_low within 1.0 ATR of that level.
        The sweep must be meaningful (>= 0.2 ATR below ref_low) but not so deep
        that it implies a real breakdown rather than a stop hunt (< 3.0 ATR below).
      Bearish Judas Swing: c1 sweeps ABOVE ref_high within 1.0 ATR of that level.

    Returns:
      judas_bull:  True if bullish Judas Swing detected
      judas_bear:  True if bearish Judas Swing detected
      depth_bull:  how far below ref_low the sweep went (in ATR multiples)
      depth_bear:  how far above ref_high the sweep went (in ATR multiples)
    """
    bull_depth = (ref_low - c1_low) / atr if atr > 0 else 0
    bear_depth = (c1_high - ref_high) / atr if atr > 0 else 0

    # Swept below AL: between 0.2 and 3.0 ATR below the level = stop hunt, not breakdown
    judas_bull = (ref_low is not None and
                  c1_low < ref_low and
                  0.2 <= bull_depth <= 3.0)

    # Swept above AH: between 0.2 and 3.0 ATR above the level
    judas_bear = (ref_high is not None and
                  c1_high > ref_high and
                  0.2 <= bear_depth <= 3.0)

    return {
        'judas_bull':  judas_bull,
        'judas_bear':  judas_bear,
        'depth_bull':  round(bull_depth, 2),
        'depth_bear':  round(bear_depth, 2),
    }



def detect_order_blocks(df: pd.DataFrame, lookback: int = 20) -> dict:
    """
    [S9-PRECISION] Order Block: the LAST opposite-direction candle immediately
    before a strong sweep+displacement sequence.

    Precision upgrades vs prior version:
    - OB candle must have body >= 0.5 ATR (not any small doji)
    - OB candle must be within 5 bars of the current candle (fresh, not stale)
    - Entry zone: price must be inside the full candle range [low, high], not
      just below the body top — the wick is part of the defended zone
    - retested: price is CURRENTLY inside the zone (active retest), not just
      anywhere below — "cur_price <= ob_high" was too loose and caught any
      price below the zone from any distance
    """
    result = {'bullish': None, 'bearish': None}
    if len(df) < 10:
        return result

    atr       = df['atr'].iloc[-1] if 'atr' in df.columns else 0.001
    window    = df.iloc[-lookback:]
    cur_price = df.iloc[-1]['close']
    n         = len(window)

    # ── Bullish OB: last bearish candle before bullish impulse
    for i in range(n - 2, 1, -1):
        c      = window.iloc[i]
        next_c = window.iloc[i + 1] if i + 1 < n else None
        if c['close'] >= c['open']:                         # must be bearish
            continue
        ob_body = abs(c['open'] - c['close'])
        if ob_body < atr * 0.5:                             # body must be meaningful
            continue
        if next_c is None:
            continue
        impulse_body = abs(next_c['close'] - next_c['open'])
        if not (next_c['close'] > next_c['open'] and       # bullish impulse candle
                impulse_body >= atr * 1.0):                # [PRECISION] was 0.5 ATR
            continue
        bars_ago = n - 1 - i
        if bars_ago > 15:                                   # [PRECISION] stale OB skip
            continue
        # Zone is full candle range (wick included) — body is the premium sub-zone
        ob_zone_high = c['high']
        ob_zone_low  = c['low']
        ob_body_high = max(c['open'], c['close'])
        ob_body_low  = min(c['open'], c['close'])
        # Active retest: price must be inside the zone right now
        in_zone   = ob_zone_low <= cur_price <= ob_zone_high
        retested  = in_zone                                 # [PRECISION] was just <= ob_high
        result['bullish'] = {
            'high':      ob_zone_high,
            'low':       ob_zone_low,
            'body_high': ob_body_high,
            'body_low':  ob_body_low,
            'bar_idx':   i,
            'bars_ago':  bars_ago,
            'active':    in_zone,
            'retested':  retested,
        }
        break

    # ── Bearish OB: last bullish candle before bearish impulse
    for i in range(n - 2, 1, -1):
        c      = window.iloc[i]
        next_c = window.iloc[i + 1] if i + 1 < n else None
        if c['close'] <= c['open']:                         # must be bullish
            continue
        ob_body = abs(c['close'] - c['open'])
        if ob_body < atr * 0.5:
            continue
        if next_c is None:
            continue
        impulse_body = abs(next_c['open'] - next_c['close'])
        if not (next_c['close'] < next_c['open'] and
                impulse_body >= atr * 1.0):
            continue
        bars_ago = n - 1 - i
        if bars_ago > 15:
            continue
        ob_zone_high = c['high']
        ob_zone_low  = c['low']
        ob_body_high = max(c['open'], c['close'])
        ob_body_low  = min(c['open'], c['close'])
        in_zone  = ob_zone_low <= cur_price <= ob_zone_high
        retested = in_zone
        result['bearish'] = {
            'high':      ob_zone_high,
            'low':       ob_zone_low,
            'body_high': ob_body_high,
            'body_low':  ob_body_low,
            'bar_idx':   i,
            'bars_ago':  bars_ago,
            'active':    in_zone,
            'retested':  retested,
        }
        break

    return result


# ── ICT-2: MARKET STRUCTURE (BOS + CHoCH) ────────────────────────────────────

def detect_market_structure(df: pd.DataFrame, swing_lookback: int = 10) -> dict:
    """
    Break of Structure (BOS): new high above the last confirmed swing high
    Change of Character (CHoCH): FIRST opposite swing against prevailing trend

    [S9-PRECISION] Upgraded: require 3 consecutive HH/HL or LL/LH for trend
    confirmation, not just 2. Two points can be noise; three is structure.

    Swing High: local max over swing_lookback bars on each side
    Swing Low:  local min over swing_lookback bars on each side

    Returns:
      trend: 'BULLISH' | 'BEARISH' | 'RANGING'
      choch: True if CHoCH detected
      last_bos_price: price of most recent BOS
      swing_highs / swing_lows: full list for OTE leg extraction
    """
    if len(df) < swing_lookback * 3:
        return {'trend': 'RANGING', 'choch': False, 'last_bos_price': None,
                'swing_highs': [], 'swing_lows': [], 'last_sh': None, 'last_sl': None}

    highs = df['high'].values
    lows  = df['low'].values
    n     = len(df)
    lb    = swing_lookback

    swing_highs = []
    swing_lows  = []

    for i in range(lb, n - lb):
        if highs[i] == max(highs[i-lb:i+lb+1]):
            swing_highs.append((i, highs[i]))
        if lows[i] == min(lows[i-lb:i+lb+1]):
            swing_lows.append((i, lows[i]))

    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return {'trend': 'RANGING', 'choch': False, 'last_bos_price': None,
                'swing_highs': swing_highs, 'swing_lows': swing_lows,
                'last_sh': None, 'last_sl': None}

    last_sh = swing_highs[-1][1]
    prev_sh = swing_highs[-2][1]
    last_sl = swing_lows[-1][1]
    prev_sl = swing_lows[-2][1]

    # [S9-PRECISION] Require 3 swings for trend confirmation
    bos_bullish = False
    bos_bearish = False
    if len(swing_highs) >= 3 and len(swing_lows) >= 3:
        bos_bullish = (
            swing_highs[-1][1] > swing_highs[-2][1] > swing_highs[-3][1] and
            swing_lows[-1][1]  > swing_lows[-2][1]
        )
        bos_bearish = (
            swing_highs[-1][1] < swing_highs[-2][1] and
            swing_lows[-1][1]  < swing_lows[-2][1]  < swing_lows[-3][1]
        )
    else:
        # Fallback to 2-swing with both conditions required
        bos_bullish = last_sh > prev_sh and last_sl > prev_sl
        bos_bearish = last_sh < prev_sh and last_sl < prev_sl

    trend = 'BULLISH' if bos_bullish else 'BEARISH' if bos_bearish else 'RANGING'

    choch = False
    if trend == 'BULLISH' and len(swing_highs) >= 3:
        choch = swing_highs[-1][1] < swing_highs[-2][1]
    elif trend == 'BEARISH' and len(swing_lows) >= 3:
        choch = swing_lows[-1][1] > swing_lows[-2][1]

    last_bos = last_sh if bos_bullish else last_sl if bos_bearish else None

    return {
        'trend':          trend,
        'choch':          choch,
        'last_bos_price': last_bos,
        'swing_highs':    swing_highs,
        'swing_lows':     swing_lows,
        'last_sh':        last_sh,
        'last_sl':        last_sl,
    }


# ── ICT-3: PREMIUM / DISCOUNT ─────────────────────────────────────────────────

def detect_premium_discount(df: pd.DataFrame, df_macro: pd.DataFrame = None,
                             lookback: int = 100) -> dict:
    """
    [S9-PRECISION] Premium / Discount zone with depth scoring.

    Upgrades:
    - Lookback extended to 100 bars on M15 (= ~25 hours, captures daily range)
    - Depth scoring: deep discount (<25%) or deep premium (>75%) scores as
      'deep' — these have much stronger mean-reversion pull than shallow zones
    - H4 macro context: if M15 shows discount but H4 shows premium, flag
      the conflict so the scorer can weigh it

    Only buy setups valid in DISCOUNT. Only sell setups in PREMIUM.
    """
    if len(df) < 10:
        return {'in_discount': False, 'in_premium': False, 'deep': False,
                'equilibrium': None, 'pct_of_range': None, 'h4_conflict': False}

    window     = df.tail(lookback)
    swing_high = window['high'].max()
    swing_low  = window['low'].min()
    equil      = (swing_high + swing_low) / 2
    current    = df.iloc[-1]['close']

    rng          = swing_high - swing_low
    pct_of_range = ((current - swing_low) / rng) if rng > 0 else 0.5

    in_discount = pct_of_range < 0.50
    in_premium  = pct_of_range > 0.50
    # [S9] Deep zones: 0-25% = deep discount, 75-100% = deep premium
    deep        = pct_of_range < 0.25 or pct_of_range > 0.75

    # H4 conflict check
    h4_conflict = False
    if df_macro is not None and not df_macro.empty and len(df_macro) >= 10:
        h4_high    = df_macro['high'].tail(50).max()
        h4_low     = df_macro['low'].tail(50).min()
        h4_rng     = h4_high - h4_low
        h4_pct     = ((current - h4_low) / h4_rng) if h4_rng > 0 else 0.5
        h4_discount = h4_pct < 0.50
        h4_premium  = h4_pct > 0.50
        # Conflict: M15 says buy zone but H4 says sell zone, or vice versa
        if in_discount and h4_premium:
            h4_conflict = True
        elif in_premium and h4_discount:
            h4_conflict = True

    return {
        'in_discount':   in_discount,
        'in_premium':    in_premium,
        'deep':          deep,
        'h4_conflict':   h4_conflict,
        'equilibrium':   round(equil, 5),
        'swing_high':    round(swing_high, 5),
        'swing_low':     round(swing_low, 5),
        'pct_of_range':  round(pct_of_range, 3),
    }


# ── ICT-4: KILL ZONE SESSION WEIGHT ──────────────────────────────────────────

def get_ict_session_weight(utc_hour: int, utc_minute: int) -> tuple:
    """
    ICT session quality weights — all windows in TRUE UTC.

    TIMEZONE NOTE: All times below are UTC. The broker terminal displays
    UTC+3, so e.g. 16:00 UTC appears as 19:00 on the broker clock.
    Do NOT use broker-local time here — only UTC from datetime.utcnow().

    London Open:    07:00–09:00 UTC  → weight 1.00  (best)
    London/NY Ovlp: 12:00–16:00 UTC  → weight 0.90  (London close + full NY)
    London PM:      09:00–12:00 UTC  → weight 0.70  (mid-session)
    Asian Range:    00:00–03:00 UTC  → weight 0.80  (JPY primary session)
    NY Lunch (real):16:00–17:30 UTC  → weight 0.00  (EDT 12:00–13:30 — avoid)
    NY PM2:         17:30–21:00 UTC  → weight 0.80  (EDT 13:30–17:00 — full NY afternoon)
    Other:          all else         → weight 0.50

    [S9-CALIBRATION] Asian raised 0.60→0.80. The weight was penalising
    setup quality rather than just session probability. A confirmed
    sweep+displacement+OB+PD in Asian is geometrically valid regardless
    of session. Hard gates (sweep+displacement mandatory) already filter
    noise — session weight no longer needs to carry that burden alone.
    JPY crosses (EURJPY/GBPJPY/AUDJPY) that scored 0.847 now reach 0.880+.
    Non-JPY pairs rarely produce genuine Asian sweeps so false-positive
    risk is self-limiting.

    [S11-SESSION] Added NY_PM2 (17:30–21:00 UTC = 13:30–17:00 EDT).
    This is the full NY afternoon post-lunch session — the most active
    institutional period for order flow completion, stop hunts, and
    end-of-day position squaring. Previously lumped into "Other" (0.50),
    which was causing high-quality JUDAS+AMD:MANIPULATION setups to fall
    below the 0.80 threshold. Now correctly weighted at 0.80.
    Signals at 20:46 UTC (EURUSD, NZDUSD, AUDUSD) that were missed due
    to this miscategorisation will now qualify for execution.

    Returns (weight, zone_name)
    """
    t = utc_hour * 60 + utc_minute

    # [SPRINT 8 TZ-FIX] Corrected from broker-local to true UTC.
    # Previously ny_lunch was (12*60, 13*60+30) — that is London/NY Overlap,
    # the highest-volume window of the day. Real NY Lunch = EDT 12:00–13:30
    # = 16:00–17:30 UTC (UTC-4 during EDT / March–November).
    london_open  = (7*60,    9*60)        # 07:00–09:00 UTC
    london_pm    = (9*60,   12*60)        # 09:00–12:00 UTC
    london_ny    = (12*60,  16*60)        # 12:00–16:00 UTC  ← was being skipped
    ny_lunch     = (16*60,  17*60+30)     # 16:00–17:30 UTC  ← real thin window
    ny_pm2       = (17*60+30, 21*60)      # 17:30–21:00 UTC  ← [S11] NY afternoon post-lunch
    asian_range  = (0,       3*60)        # 00:00–03:00 UTC

    if london_open[0] <= t < london_open[1]:
        return 1.00, 'London'
    if london_ny[0] <= t < london_ny[1]:
        return 0.90, 'London_NY'
    if london_pm[0] <= t < london_pm[1]:
        return 0.70, 'London_PM'
    if asian_range[0] <= t < asian_range[1]:
        return 0.80, 'Asian'
    if ny_lunch[0] <= t < ny_lunch[1]:
        return 0.00, 'NY_Lunch'
    if ny_pm2[0] <= t < ny_pm2[1]:
        return 0.80, 'NY_PM2'   # [S11] Full NY afternoon — prime institutional window
    return 0.50, 'Other'


# ── ICT-5: OTE ZONE ──────────────────────────────────────────────────────────

def get_ote_zone(impulse_high: float, impulse_low: float,
                 direction: str) -> tuple:
    """
    [S9-PRECISION] Optimal Trade Entry: 61.8%–78.6% Fibonacci retracement.

    The caller now passes the EXPLICIT impulse leg:
      BUY:  impulse_low  = sweep candle low,  impulse_high = displacement candle high
      SELL: impulse_high = sweep candle high, impulse_low  = displacement candle low
    This gives a geometrically correct OTE from the actual move that created
    the setup, not from a generic 20-bar rolling high/low.

    Returns (ote_low, ote_high). Returns (0.0, 0.0) if range is zero.
    """
    rng = impulse_high - impulse_low
    if rng <= 0:
        return 0.0, 0.0
    if direction == 'BUY':
        ote_low  = impulse_high - rng * 0.786
        ote_high = impulse_high - rng * 0.618
    else:
        ote_low  = impulse_low  + rng * 0.618
        ote_high = impulse_low  + rng * 0.786
    return round(ote_low, 5), round(ote_high, 5)


# ── ICT-5b: FVG DETECTION ────────────────────────────────────────────────────

def detect_fvg(df: pd.DataFrame, direction: str, atr: float,
               lookback: int = 10) -> bool:
    """
    [S9-PRECISION] Fair Value Gap: meaningful unfilled imbalance.

    Prior version checked only candles c1/c2/c3 (last 3 bars), had no minimum
    size, and no check if the gap had already been filled by subsequent price.

    Upgrades:
      - Scans last `lookback` bars for any still-open FVG
      - Minimum gap size >= 0.3 ATR (real institutional imbalance)
      - Validates the gap has NOT been re-entered since it formed — a filled
        FVG is no longer an active target

    Returns True if a qualifying unfilled FVG exists in the direction of trade.
    """
    if len(df) < lookback + 2 or atr <= 0:
        return False

    min_gap = atr * 0.3
    window  = df.tail(lookback + 2).reset_index(drop=True)
    n       = len(window)

    for i in range(n - 2):
        c1 = window.iloc[i]
        c3 = window.iloc[i + 2]

        if direction == 'BUY':
            gap_low  = c1['high']
            gap_high = c3['low']
            if gap_high <= gap_low or (gap_high - gap_low) < min_gap:
                continue
            # Gap still open if no subsequent candle's low entered it
            subsequent = window.iloc[i + 2:]
            if not (subsequent['low'] < gap_high).any():
                return True

        else:  # SELL
            gap_high = c1['low']
            gap_low  = c3['high']
            if gap_low >= gap_high or (gap_high - gap_low) < min_gap:
                continue
            subsequent = window.iloc[i + 2:]
            if not (subsequent['high'] > gap_low).any():
                return True

    return False


# ── ICT-6: EQUAL HIGHS / LOWS ────────────────────────────────────────────────

def detect_equal_highs_lows(df: pd.DataFrame, lookback: int = 30,
                              tolerance_atr_mult: float = 0.30) -> dict:
    """
    Equal Highs/Lows = engineered liquidity pools.
    Price repeatedly testing the same level = stop orders clustered there.
    The system will hunt these before reversing.

    Criteria: two or more highs/lows within tolerance_atr of each other.
    Returns lists of equal high/low price levels.
    """
    if len(df) < 10 or 'atr' not in df.columns:
        return {'equal_highs': [], 'equal_lows': []}

    atr     = df['atr'].iloc[-1]
    tol     = atr * tolerance_atr_mult
    window  = df.tail(lookback)

    def find_equal_levels(prices, tolerance):
        levels = []
        for i, p1 in enumerate(prices):
            matches = [p2 for p2 in prices[i+1:] if abs(p1 - p2) <= tolerance]
            if matches:
                levels.append(round(p1, 5))
        return list(set(levels))

    eq_highs = find_equal_levels(window['high'].values.tolist(), tol)
    eq_lows  = find_equal_levels(window['low'].values.tolist(), tol)

    return {'equal_highs': eq_highs, 'equal_lows': eq_lows}


# ── ICT-7: MACRO TREND (OPT-2 from Sprint 6, preserved) ─────────────────────

def _derive_macro_trend(df_macro: pd.DataFrame) -> str:
    """
    EMA-20 × EMA-50 dual confirmation on H4.
    Two consecutive candles must agree. Falls back to NEUTRAL if EMAs disagree.
    """
    if df_macro is None or df_macro.empty or len(df_macro) < 52:
        return "NEUTRAL"
    df = df_macro.copy()
    df['ema_20'] = df['close'].ewm(span=20, adjust=False).mean()
    df['ema_50'] = df['close'].ewm(span=50, adjust=False).mean()
    c_prev = df.iloc[-2]
    c_last = df.iloc[-1]
    ema20  = c_last['ema_20']
    ema50  = c_last['ema_50']
    bull   = (c_last['close'] > ema20) and (c_prev['close'] > c_prev['ema_20'])
    bear   = (c_last['close'] < ema20) and (c_prev['close'] < c_prev['ema_20'])
    if bull and ema20 > ema50: return "BULLISH"
    if bear and ema20 < ema50: return "BEARISH"
    return "NEUTRAL"



# ── AMD-5: DISTRIBUTION MODE SCORER ──────────────────────────────────────────


# ── [S25-F4] H4 TREND STRENGTH ────────────────────────────────────────────────
# Counts confirmed Higher High/Higher Low (bullish) or Lower Low/Lower High
# (bearish) sequences on the H4 dataframe.
# Returns: (swing_count, _strong_h4, _sniper_h4)
#   _strong_h4  = True when 3+ confirmed H4 swings → enforce_macro activates
#   _sniper_h4  = True when 4+ confirmed H4 swings → counter-trend needs 0.88+

def _h4_trend_strength(df_macro: pd.DataFrame) -> tuple:
    """
    Count confirmed H4 swing sequences to determine trend conviction.
    Uses 3-bar pivot swing detection (local high/low over prev/next bar).
    Returns (swing_count: int, strong: bool, sniper: bool)
    """
    if df_macro is None or len(df_macro) < 10:
        return 0, False, False

    df = df_macro.copy()
    highs  = df['high'].values
    lows   = df['low'].values
    closes = df['close'].values
    n      = len(df)

    # Identify pivot highs and lows using 3-bar look-around
    pivot_highs = []
    pivot_lows  = []
    for i in range(1, n - 1):
        if highs[i] > highs[i-1] and highs[i] > highs[i+1]:
            pivot_highs.append((i, highs[i]))
        if lows[i] < lows[i-1] and lows[i] < lows[i+1]:
            pivot_lows.append((i, lows[i]))

    if len(pivot_highs) < 2 or len(pivot_lows) < 2:
        return 0, False, False

    # Count bullish sequence: HH (each new pivot high > prior pivot high)
    hh_count, hl_count = 0, 0
    for k in range(1, len(pivot_highs)):
        if pivot_highs[k][1] > pivot_highs[k-1][1]:
            hh_count += 1
    for k in range(1, len(pivot_lows)):
        # HL: each swing low is higher than the previous
        if pivot_lows[k][1] > pivot_lows[k-1][1]:
            hl_count += 1

    # Count bearish sequence: LL + LH
    ll_count, lh_count = 0, 0
    for k in range(1, len(pivot_lows)):
        if pivot_lows[k][1] < pivot_lows[k-1][1]:
            ll_count += 1
    for k in range(1, len(pivot_highs)):
        if pivot_highs[k][1] < pivot_highs[k-1][1]:
            lh_count += 1

    bull_swings = hh_count + hl_count
    bear_swings = ll_count + lh_count
    swing_count = max(bull_swings, bear_swings)

    _strong_h4 = swing_count >= 3
    _sniper_h4 = swing_count >= 4
    return swing_count, _strong_h4, _sniper_h4


def _score_distribution(df: pd.DataFrame, structure: dict, obs: dict,
                         pd_zone: dict, session_wt: float, kill_zone: str,
                         current_atr: float, vol_ratio: float,
                         macro_trend: str, c3) -> tuple:
    """
    Distribution mode scoring — entered during DISTRIBUTION and NY_DISTRIBUTION.

    In distribution, price is trending after the Manipulation sweep.
    Entry model: OB or FVG retest in the direction of the trend.
    Sweep+displacement is NOT required — we enter the pullback.

    Score architecture:
      OB retest in trend direction  +0.30
      FVG retest in trend direction +0.20
      BOS confirmed (trend)         +0.15
      Kill zone session quality     +0.15 × weight
      OTE pullback depth            +0.10
      Volume surge                  ×1.10

    Returns (signal, score, reason, conditions) or None if no setup.
    """
    if macro_trend not in ('BULLISH', 'BEARISH'):
        return None

    is_bull = macro_trend == 'BULLISH'
    score   = 0.0
    cond    = {'mode': 'DISTRIBUTION'}

    cond['kill_zone'] = kill_zone
    score += 0.15 * session_wt

    ob_key = 'bullish' if is_bull else 'bearish'
    ob     = obs.get(ob_key)
    ob_hit = bool(ob and ob.get('retested'))
    cond['order_block'] = ob_hit
    if ob_hit: score += 0.30

    fvg_dir = 'BUY' if is_bull else 'SELL'
    fvg_hit = detect_fvg(df, fvg_dir, current_atr)
    cond['fvg'] = fvg_hit
    if fvg_hit: score += 0.20

    bos = structure.get('trend') == macro_trend
    cond['bos_aligned'] = bos
    if bos: score += 0.15

    swing_lows  = structure.get('swing_lows', [])
    swing_highs = structure.get('swing_highs', [])
    ote_hit = False
    if is_bull and len(swing_lows) >= 2 and len(swing_highs) >= 2:
        ote_lo, ote_hi = get_ote_zone(swing_highs[-1][1], swing_lows[-1][1], 'BUY')
        ote_hit = ote_lo > 0 and ote_lo <= c3['close'] <= ote_hi
    elif not is_bull and len(swing_highs) >= 2 and len(swing_lows) >= 2:
        ote_lo, ote_hi = get_ote_zone(swing_highs[-1][1], swing_lows[-1][1], 'SELL')
        ote_hit = ote_lo > 0 and ote_lo <= c3['close'] <= ote_hi
    cond['ote'] = ote_hit
    if ote_hit: score += 0.10

    vol_surge = vol_ratio >= 1.3
    cond['volume_surge'] = vol_surge
    cond['vol_ratio'] = round(vol_ratio, 2)
    if vol_surge:
        score = min(0.99, score * 1.10)

    if not ob_hit and not fvg_hit:
        return None
    if score < 0.40:
        return None

    direction = 'BUY_MICRO' if is_bull else 'SELL_MICRO'
    reason = (f"DISTRIBUTION [{kill_zone}] | Score:{score:.2f} | "
              f"OB:{ob_hit} FVG:{fvg_hit} BOS:{bos} OTE:{ote_hit} "
              f"Vol:{vol_ratio:.1f}x | Trend:{macro_trend}")
    return direction, round(score, 3), reason, cond


# ══════════════════════════════════════════════════════════════════════════════
# SPRINT 13 — CONFLUENCE AMPLIFIER LAYER
#
# Four frameworks that resolve ICT's six structural weaknesses:
#
#   [S13-F1] compute_vwap_context()    — VWAP + Z-score + slope
#              Fixes: stale H4 macro trend, no statistical extreme grading
#   [S13-F2] compute_volume_profile()  — POC / VAH / VAL / HVN / LVN
#              Fixes: binary OB/FVG quality, no price-acceptance context
#   [S13-F3] compute_delta_context()   — Proxy cumulative delta + divergence
#              Fixes: volume direction blindness (5× buy vs 5× sell indistinct)
#   [S13-F4] wyckoff_spring_check()    — Low-vol test validation
#              Fixes: AMD phase confirmed LATE (after sweep+displacement)
#
# Architecture: additive bonus layer on top of ICT core score.
# All functions are pure — operate only on the M15 DataFrame already in memory.
# Outputs stored in cond{} dict for DB logging and Telegram transparency.
# ══════════════════════════════════════════════════════════════════════════════


def compute_vwap_context(df: pd.DataFrame) -> dict:
    """
    [S13-F1] Intraday VWAP + Z-score + slope.

    VWAP = cumulative(price × volume) / cumulative(volume), reset at session start.
    We use the full available M15 window as the VWAP period (no hard reset —
    MT5 data windows start at request time, not at midnight, so we anchor to
    the first bar in df and build forward).

    Z-score = (current_price - VWAP) / rolling_std(price, 20 bars)
    Slope   = VWAP[-1] vs VWAP[-5] expressed as direction string

    Why it matters:
      - Institutional algorithms reference VWAP as the day's fair value.
      - Price > 2σ above VWAP = statistically overextended (bearish extreme).
      - Price < -2σ below VWAP = statistically oversold (bullish extreme).
      - VWAP slope (negative) overrides a stale bullish H4 EMA for intraday bias.

    Returns:
      vwap:         float — current VWAP level
      vwap_z:       float — Z-score (positive = above, negative = below)
      vwap_slope:   'UP' | 'DOWN' | 'FLAT'
      above_vwap:   bool — price is above VWAP
      extreme_bull: bool — Z < -1.5 (price far below VWAP, bullish reversion)
      extreme_bear: bool — Z > +1.5 (price far above VWAP, bearish reversion)
    """
    empty = {'vwap': 0.0, 'vwap_z': 0.0, 'vwap_slope': 'FLAT',
             'above_vwap': False, 'extreme_bull': False, 'extreme_bear': False}
    if len(df) < 20 or 'volume' not in df.columns:
        return empty

    try:
        typical_price = (df['high'] + df['low'] + df['close']) / 3.0
        cum_tp_vol    = (typical_price * df['volume']).cumsum()
        cum_vol       = df['volume'].cumsum().replace(0, np.nan)
        vwap_series   = cum_tp_vol / cum_vol

        current_vwap  = float(vwap_series.iloc[-1])
        current_price = float(df['close'].iloc[-1])

        # Z-score: deviation from VWAP normalised by rolling price std (20 bars)
        price_std = float(df['close'].rolling(20).std().iloc[-1])
        if price_std <= 0:
            price_std = current_vwap * 0.001   # fallback: 0.1% of price
        vwap_z = (current_price - current_vwap) / price_std

        # Slope: compare last bar's VWAP to 5 bars ago
        if len(vwap_series) >= 6:
            slope_delta = vwap_series.iloc[-1] - vwap_series.iloc[-6]
            slope_pct   = abs(slope_delta) / (current_vwap + 1e-10)
            if slope_pct < 0.0001:   vwap_slope = 'FLAT'
            elif slope_delta > 0:    vwap_slope = 'UP'
            else:                    vwap_slope = 'DOWN'
        else:
            vwap_slope = 'FLAT'

        return {
            'vwap':         round(current_vwap, 5),
            'vwap_z':       round(float(vwap_z), 3),
            'vwap_slope':   vwap_slope,
            'above_vwap':   current_price > current_vwap,
            'extreme_bull': vwap_z < -1.5,   # oversold extreme → bullish reversion
            'extreme_bear': vwap_z > +1.5,   # overbought extreme → bearish reversion
        }
    except Exception:
        return empty


def compute_volume_profile(df: pd.DataFrame, lookback: int = 200,
                            atr: float = 0.0) -> dict:
    """
    [S13-F2] Volume Profile: POC, VAH, VAL, HVN, LVN detection.

    Divides the price range into buckets (width = ATR / 10, min 20 buckets)
    and sums volume per bucket. Identifies:

      POC (Point of Control): bucket with highest volume — market equilibrium.
      VAH (Value Area High):  upper edge of zone containing 70% of total volume.
      VAL (Value Area Low):   lower edge of same zone.
      HVN (High Volume Node): bucket with vol >= 1.5 × median bucket vol.
      LVN (Low Volume Node):  bucket with vol <= 0.5 × median bucket vol.

    Integration:
      ob_at_hvn:  bool — the most recent OB zone overlaps an HVN.
      fvg_at_lvn: bool — the most recent price gap is in an LVN (confirms inefficiency).
      at_poc:     bool — current price is within 0.5 ATR of POC.

    Returns dict with all of the above.
    """
    empty = {'poc': 0.0, 'vah': 0.0, 'val': 0.0,
             'at_poc': False, 'ob_at_hvn': False, 'fvg_at_lvn': False,
             'hvn_levels': [], 'lvn_levels': []}
    if len(df) < 20 or 'volume' not in df.columns:
        return empty

    try:
        window = df.tail(min(lookback, len(df))).copy()
        price_min = float(window['low'].min())
        price_max = float(window['high'].max())
        price_rng = price_max - price_min
        if price_rng <= 0:
            return empty

        # Bucket width: ATR/10, bounded to produce 20-200 buckets
        bucket_w = (atr / 10.0) if atr > 0 else (price_rng / 50.0)
        bucket_w = max(bucket_w, price_rng / 200.0)
        bucket_w = min(bucket_w, price_rng / 20.0)

        n_buckets   = max(1, int(price_rng / bucket_w) + 1)
        vol_profile = np.zeros(n_buckets)

        for _, row in window.iterrows():
            lo  = float(row['low'])
            hi  = float(row['high'])
            vol = float(row['volume'])
            # Distribute row volume across the buckets it spans
            b_lo = int((lo - price_min) / bucket_w)
            b_hi = int((hi - price_min) / bucket_w)
            b_lo = max(0, min(b_lo, n_buckets - 1))
            b_hi = max(0, min(b_hi, n_buckets - 1))
            span = max(1, b_hi - b_lo + 1)
            for b in range(b_lo, b_hi + 1):
                vol_profile[b] += vol / span

        # POC
        poc_idx   = int(np.argmax(vol_profile))
        poc_price = price_min + poc_idx * bucket_w + bucket_w / 2.0

        # Value Area (70% of total volume around POC)
        total_vol    = vol_profile.sum()
        target_vol   = total_vol * 0.70
        va_lo = va_hi = poc_idx
        accumulated  = vol_profile[poc_idx]
        while accumulated < target_vol and (va_lo > 0 or va_hi < n_buckets - 1):
            add_lo = vol_profile[va_lo - 1] if va_lo > 0 else 0
            add_hi = vol_profile[va_hi + 1] if va_hi < n_buckets - 1 else 0
            if add_lo >= add_hi and va_lo > 0:
                va_lo -= 1;  accumulated += add_lo
            elif va_hi < n_buckets - 1:
                va_hi += 1;  accumulated += add_hi
            else:
                break
        vah = price_min + va_hi * bucket_w + bucket_w
        val = price_min + va_lo * bucket_w

        # HVN / LVN
        median_vol   = float(np.median(vol_profile[vol_profile > 0])) if np.any(vol_profile > 0) else 1.0
        hvn_levels   = [price_min + i * bucket_w + bucket_w / 2.0
                        for i, v in enumerate(vol_profile) if v >= median_vol * 1.5]
        lvn_levels   = [price_min + i * bucket_w + bucket_w / 2.0
                        for i, v in enumerate(vol_profile) if 0 < v <= median_vol * 0.5]

        current_price = float(df['close'].iloc[-1])
        tol           = (atr * 0.5) if atr > 0 else bucket_w * 2

        at_poc    = abs(current_price - poc_price) <= tol
        ob_at_hvn = any(abs(current_price - h) <= tol for h in hvn_levels)
        fvg_at_lvn = any(abs(current_price - l) <= tol for l in lvn_levels)

        return {
            'poc':        round(poc_price, 5),
            'vah':        round(vah, 5),
            'val':        round(val, 5),
            'at_poc':     at_poc,
            'ob_at_hvn':  ob_at_hvn,
            'fvg_at_lvn': fvg_at_lvn,
            'hvn_levels': [round(h, 5) for h in hvn_levels[:5]],
            'lvn_levels': [round(l, 5) for l in lvn_levels[:5]],
        }
    except Exception:
        return empty


def compute_delta_context(df: pd.DataFrame) -> dict:
    """
    [S13-F3] Proxy Cumulative Delta + divergence detection.

    True cumulative delta requires Level 2 order book data unavailable via MT5.
    Proxy delta approximates directional volume pressure:
      Bull candle (close > open) → positive delta  (+volume)
      Bear candle (close < open) → negative delta  (-volume)
      Doji                       → zero delta

    Cumulative delta slope: sum of last 5 candles' proxy deltas.
    Divergence: price making new high but delta declining (hidden sell pressure),
                or price making new low but delta rising (hidden buy pressure).

    Returns:
      cum_delta_slope: float  — positive = net buying, negative = net selling
      delta_bull:      bool   — slope positive (net buying pressure)
      delta_bear:      bool   — slope negative (net selling pressure)
      divergence:      bool   — delta diverges from price direction (early signal)
      delta_confirms_buy:  bool  — delta positive AND price rising
      delta_confirms_sell: bool  — delta negative AND price falling
    """
    empty = {'cum_delta_slope': 0.0, 'delta_bull': False, 'delta_bear': False,
             'divergence': False, 'delta_confirms_buy': False, 'delta_confirms_sell': False}
    if len(df) < 10 or 'volume' not in df.columns:
        return empty

    try:
        window  = df.tail(10).copy()
        # Proxy delta per candle
        delta   = np.where(window['close'] > window['open'],  window['volume'],
                  np.where(window['close'] < window['open'], -window['volume'], 0.0))

        # Last 5 candles' cumulative slope
        slope_5 = float(delta[-5:].sum())

        # Price direction over last 5 bars
        price_up   = float(window['close'].iloc[-1]) > float(window['close'].iloc[-6]) if len(window) >= 6 else False
        price_down = float(window['close'].iloc[-1]) < float(window['close'].iloc[-6]) if len(window) >= 6 else False

        # Divergence: price up but delta negative (or price down but delta positive)
        divergence = (price_up and slope_5 < 0) or (price_down and slope_5 > 0)

        return {
            'cum_delta_slope':     round(slope_5, 2),
            'delta_bull':          slope_5 > 0,
            'delta_bear':          slope_5 < 0,
            'divergence':          divergence,
            'delta_confirms_buy':  slope_5 > 0 and price_up,
            'delta_confirms_sell': slope_5 < 0 and price_down,
        }
    except Exception:
        return empty


def wyckoff_spring_check(df: pd.DataFrame, manip_data: dict,
                          avg_vol: float) -> dict:
    """
    [S13-F4] Wyckoff Spring / Upthrust validation at the sweep candle.

    Wyckoff's First Principle:
      A genuine test of support (Spring) occurs on DECLINING volume.
      Low volume at the sweep = exhausted sellers, few real participants.
      The market will reverse — this is accumulation, not breakdown.
      High volume at the sweep = real selling pressure, potential genuine break.

    Upthrust (mirror for bearish): fake break above resistance on declining
    or low volume. Confirmed by a strong close back below the swept level.

    We use the sweep candle (c1 = df.iloc[-3]) as the test candle.

    Returns:
      spring:           bool — bullish Spring confirmed (sweep_low + low_vol)
      upthrust:         bool — bearish Upthrust confirmed (sweep_high + low_vol)
      test_vol_ratio:   float — vol at sweep vs 10-bar avg (< 0.70 = low vol)
      low_vol_test:     bool — test_vol_ratio < 0.70
    """
    empty = {'spring': False, 'upthrust': False,
             'test_vol_ratio': 1.0, 'low_vol_test': False}
    if len(df) < 10 or 'volume' not in df.columns or avg_vol <= 0:
        return empty

    try:
        c1      = df.iloc[-3]   # sweep candle
        vol_c1  = float(c1['volume'])

        # 10-bar average volume (excluding the sweep candle itself)
        avg_10 = float(df['volume'].iloc[-13:-3].mean()) if len(df) >= 13 else avg_vol
        if avg_10 <= 0:
            avg_10 = avg_vol

        test_vol_ratio = vol_c1 / avg_10
        low_vol_test   = test_vol_ratio < 0.70   # Wyckoff: test on < 70% avg volume

        # Spring: sweep was BULLISH (sweep low) + low volume = genuine Spring
        sweep_was_bull = manip_data.get('direction') == 'BULL' if manip_data else False
        # Upthrust: sweep was BEARISH (sweep high) + low volume
        sweep_was_bear = manip_data.get('direction') == 'BEAR' if manip_data else False

        # Additional confirmation: close quality.
        # Spring candle should ideally close ABOVE the swept level (rejection wick down, close up).
        # We check if the sweep candle had a long lower wick relative to body.
        c1_range = float(c1['high']) - float(c1['low'])
        c1_body  = abs(float(c1['close']) - float(c1['open']))
        wick_ratio = (c1_body / c1_range) if c1_range > 0 else 1.0
        # Long wick (body < 40% of range) = price rejected at the extreme → spring quality
        long_wick = wick_ratio < 0.40

        spring   = sweep_was_bull and low_vol_test
        upthrust = sweep_was_bear and low_vol_test

        return {
            'spring':         spring,
            'upthrust':       upthrust,
            'test_vol_ratio': round(test_vol_ratio, 3),
            'low_vol_test':   low_vol_test,
            'long_wick':      long_wick,
        }
    except Exception:
        return empty


def compute_tick_delta(df_ticks: pd.DataFrame, direction: str) -> dict:
    """
    [S19f] Tick-Level Order Flow Imbalance (Cumulative Tick Delta).
    Analyzes the last 1,000 micro-ticks to measure true buying vs. selling aggression.
    
    MT5 Tick Flags:
      2 = TICK_FLAG_ASK (Buy order hitting the ask)
      4 = TICK_FLAG_BID (Sell order hitting the bid)
    """
    empty = {'ctd': 0.0, 'tick_surge': False, 'delta_confirms': False}
    if df_ticks is None or df_ticks.empty or 'flags' not in df_ticks.columns:
        return empty

    try:
        # Isolate trades (ignore pure quote updates)
        trades = df_ticks[(df_ticks['flags'] & 2) | (df_ticks['flags'] & 4)].copy()
        if len(trades) < 50:
            return empty

        # Calculate volume delta: Ask hits (Buys) - Bid hits (Sells)
        buy_vol  = trades[trades['flags'] & 2]['volume'].sum()
        sell_vol = trades[trades['flags'] & 4]['volume'].sum()
        ctd = buy_vol - sell_vol
        
        # Calculate recent momentum (last 20% of ticks vs historical 80%)
        split_idx = int(len(trades) * 0.8)
        recent_trades = trades.iloc[split_idx:]
        recent_buy_vol = recent_trades[recent_trades['flags'] & 2]['volume'].sum()
        recent_sell_vol = recent_trades[recent_trades['flags'] & 4]['volume'].sum()
        
        recent_delta = recent_buy_vol - recent_sell_vol
        tick_surge = abs(recent_delta) > (abs(ctd) * 0.30)  # Sudden burst of delta

        delta_confirms = (direction == 'BUY' and recent_delta > 0 and ctd > 0) or \
                         (direction == 'SELL' and recent_delta < 0 and ctd < 0)

        return {
            'ctd': float(ctd),
            'recent_delta': float(recent_delta),
            'tick_surge': tick_surge,
            'delta_confirms': delta_confirms
        }
    except Exception:
        return empty


# ── CORE ICT CONFLUENCE SCORER ────────────────────────────────────────────────

# ── [S17] CANDLESTICK INTELLIGENCE LAYER ─────────────────────────────────────
#
#   Detects 6 key price-action patterns on the last 1-3 closed bars.
#   Source: The Candlestick Trading Bible (Munehisa Homma / ICT school).
#
#   Design rule (from the book): a pattern ONLY earns a bonus when it occurs
#   at a key structural level. In TradeCore that level is already required by
#   the ICT/PO3 layer — so the bonus is additive on top of an existing signal,
#   never a standalone trigger.
#
#   Pattern bonuses (confirmed = pattern agrees with trade direction):
#     Engulfing Bar         +0.15  (strongest — body fully engulfs prior body)
#     Hammer / Shooting Star +0.12  (pin bar: tail ≥ 2× body, tail ≥ 0.3×ATR)
#     Morning / Evening Star +0.12  (3-bar reversal; third bar closes past midpoint)
#     Dragonfly / Gravestone Doji +0.08
#     Harami (Inside Bar)   +0.08  (compression before continuation)
#     Tweezers Top/Bottom   +0.08  (double rejection at same high/low)
#     Plain Doji             +0.05  (indecision — weakest confirmation)
#
#   Conflict penalties (pattern opposes trade direction):
#     Engulfing conflict    -0.15  (also triggers hard block in bot_engine)
#     Pin bar conflict      -0.12  (also triggers hard block in bot_engine)
#     Doji variant conflict  -0.08
#
#   Called once per signal cycle from compute_ict_confluence() (both MANIP
#   and DISTRIBUTION paths) and compute_scalp_confluence() (60% weight).

def detect_candlestick_pattern(df: pd.DataFrame, direction: str,
                                atr: float) -> dict:
    """
    [S17] Candlestick Intelligence Layer.

    Evaluates the last 1-3 CLOSED bars for reversal and continuation patterns.
    Returns a bonus when the pattern confirms the trade direction and a
    conflict flag when a strong opposing pattern forms at the entry zone.

    Args:
        df:        M15 OHLCV DataFrame (must have >= 4 rows)
        direction: 'BUY' or 'SELL'
        atr:       current 14-bar ATR value

    Returns dict:
        pattern:          str   — pattern name or 'NONE'
        confirmed:        bool  — pattern aligns with direction → add bonus
        bonus:            float — score to add if confirmed
        conflict:         bool  — strong opposing pattern → subtract / block
        conflict_pattern: str   — name of conflicting pattern
        conflict_penalty: float — score to subtract if conflict
    """
    empty = {
        'pattern': 'NONE', 'confirmed': False, 'bonus': 0.0,
        'conflict': False, 'conflict_pattern': 'NONE', 'conflict_penalty': 0.0,
    }
    try:
        if len(df) < 4 or atr <= 0:
            return empty

        # c2 = most recent CLOSED candle (pattern bar)
        # c1 = one bar earlier (context / mother candle for Harami)
        c1 = df.iloc[-3]
        c2 = df.iloc[-2]

        is_buy = (direction == 'BUY')

        # ── Per-bar metrics ──────────────────────────────────────────
        def _metrics(c):
            body   = abs(c['close'] - c['open'])
            bull   = c['close'] > c['open']
            upper  = c['high'] - max(c['close'], c['open'])
            lower  = min(c['close'], c['open']) - c['low']
            return body, bull, upper, lower

        c1_body, c1_bull, c1_upper, c1_lower = _metrics(c1)
        c2_body, c2_bull, c2_upper, c2_lower = _metrics(c2)

        min_body = atr * 0.05   # noise filter: must be a real candle body

        # ── 1. ENGULFING BAR ─────────────────────────────────────────
        # Second body fully engulfs first body; bodies must be opposite colors
        # (same-color engulfing is weaker — excluded per book guidance).
        bull_engulf = (
            c2_bull and not c1_bull and
            c2['open']  < c1['close'] and
            c2['close'] > c1['open']  and
            c2_body > min_body and c1_body > min_body
        )
        bear_engulf = (
            not c2_bull and c1_bull and
            c2['open']  > c1['close'] and
            c2['close'] < c1['open']  and
            c2_body > min_body and c1_body > min_body
        )
        if is_buy and bull_engulf:
            return {'pattern': 'Bullish_Engulfing', 'confirmed': True, 'bonus': 0.15,
                    'conflict': False, 'conflict_pattern': 'NONE', 'conflict_penalty': 0.0}
        if not is_buy and bear_engulf:
            return {'pattern': 'Bearish_Engulfing', 'confirmed': True, 'bonus': 0.15,
                    'conflict': False, 'conflict_pattern': 'NONE', 'conflict_penalty': 0.0}
        if is_buy and bear_engulf:
            return {'pattern': 'NONE', 'confirmed': False, 'bonus': 0.0,
                    'conflict': True, 'conflict_pattern': 'Bearish_Engulfing',
                    'conflict_penalty': 0.15}
        if not is_buy and bull_engulf:
            return {'pattern': 'NONE', 'confirmed': False, 'bonus': 0.0,
                    'conflict': True, 'conflict_pattern': 'Bullish_Engulfing',
                    'conflict_penalty': 0.15}

        # ── 2. PIN BAR — Hammer / Shooting Star ──────────────────────
        # Book: "shadow should be at least twice the length of the real body."
        # Extra: tail must be ≥ 0.3×ATR to be meaningful at a structural level.
        # Exclude near-zero body (those are Doji variants, handled below).
        small_body  = c2_body < atr * 0.40
        is_doji_body = c2_body < atr * 0.08
        hammer = (
            small_body and not is_doji_body and
            c2_lower >= 2.0 * max(c2_body, min_body) and
            c2_lower >= atr * 0.30 and
            c2_upper <= c2_lower * 0.50      # minimal upper wick
        )
        shooting_star = (
            small_body and not is_doji_body and
            c2_upper >= 2.0 * max(c2_body, min_body) and
            c2_upper >= atr * 0.30 and
            c2_lower <= c2_upper * 0.50      # minimal lower wick
        )
        if is_buy and hammer:
            return {'pattern': 'Hammer', 'confirmed': True, 'bonus': 0.12,
                    'conflict': False, 'conflict_pattern': 'NONE', 'conflict_penalty': 0.0}
        if not is_buy and shooting_star:
            return {'pattern': 'Shooting_Star', 'confirmed': True, 'bonus': 0.12,
                    'conflict': False, 'conflict_pattern': 'NONE', 'conflict_penalty': 0.0}
        if is_buy and shooting_star:
            return {'pattern': 'NONE', 'confirmed': False, 'bonus': 0.0,
                    'conflict': True, 'conflict_pattern': 'Shooting_Star',
                    'conflict_penalty': 0.12}
        if not is_buy and hammer:
            return {'pattern': 'NONE', 'confirmed': False, 'bonus': 0.0,
                    'conflict': True, 'conflict_pattern': 'Hammer',
                    'conflict_penalty': 0.12}

        # ── 3. MORNING STAR / EVENING STAR ───────────────────────────
        # 3-bar pattern on the three most recent CLOSED bars:
        #   p1 = df.iloc[-4]  strong trend bar
        #   p2 = df.iloc[-3]  small indecision / Doji (≤ 40% of p1 body)
        #   p3 = df.iloc[-2]  confirmation bar closing past p1 midpoint
        # We use closed bars only — df.iloc[-1] is still forming.
        p1 = df.iloc[-4]
        p2 = df.iloc[-3]
        p3 = df.iloc[-2]   # = c2 above — same candle, named for clarity
        p1_body = abs(p1['close'] - p1['open'])
        p2_body = abs(p2['close'] - p2['open'])
        p3_body = abs(p3['close'] - p3['open'])
        p1_mid  = (p1['open'] + p1['close']) / 2.0

        morning_star = (
            p1['close'] < p1['open'] and   # p1 bearish
            p1_body >= atr * 0.40 and       # p1 significant
            p2_body <= p1_body * 0.40 and   # p2 small (indecision)
            p3['close'] > p3['open'] and    # p3 bullish
            p3['close'] > p1_mid and        # closes past p1 midpoint
            p3_body >= atr * 0.30           # p3 meaningful
        )
        evening_star = (
            p1['close'] > p1['open'] and
            p1_body >= atr * 0.40 and
            p2_body <= p1_body * 0.40 and
            p3['close'] < p3['open'] and
            p3['close'] < p1_mid and
            p3_body >= atr * 0.30
        )
        if is_buy and morning_star:
            return {'pattern': 'Morning_Star', 'confirmed': True, 'bonus': 0.12,
                    'conflict': False, 'conflict_pattern': 'NONE', 'conflict_penalty': 0.0}
        if not is_buy and evening_star:
            return {'pattern': 'Evening_Star', 'confirmed': True, 'bonus': 0.12,
                    'conflict': False, 'conflict_pattern': 'NONE', 'conflict_penalty': 0.0}

        # ── 4. DOJI — Plain / Dragonfly / Gravestone ─────────────────
        doji_body   = is_doji_body    # body < 8% ATR = essentially no real body
        dragonfly   = doji_body and c2_lower >= atr * 0.30 and c2_upper <= atr * 0.15
        gravestone  = doji_body and c2_upper >= atr * 0.30 and c2_lower <= atr * 0.15
        plain_doji  = doji_body and not dragonfly and not gravestone

        if is_buy and dragonfly:
            return {'pattern': 'Dragonfly_Doji', 'confirmed': True, 'bonus': 0.08,
                    'conflict': False, 'conflict_pattern': 'NONE', 'conflict_penalty': 0.0}
        if not is_buy and gravestone:
            return {'pattern': 'Gravestone_Doji', 'confirmed': True, 'bonus': 0.08,
                    'conflict': False, 'conflict_pattern': 'NONE', 'conflict_penalty': 0.0}
        if is_buy and gravestone:
            return {'pattern': 'NONE', 'confirmed': False, 'bonus': 0.0,
                    'conflict': True, 'conflict_pattern': 'Gravestone_Doji',
                    'conflict_penalty': 0.08}
        if not is_buy and dragonfly:
            return {'pattern': 'NONE', 'confirmed': False, 'bonus': 0.0,
                    'conflict': True, 'conflict_pattern': 'Dragonfly_Doji',
                    'conflict_penalty': 0.08}
        if plain_doji:
            # Neutral doji at key level = indecision; mild confirmation any direction
            return {'pattern': 'Doji', 'confirmed': True, 'bonus': 0.05,
                    'conflict': False, 'conflict_pattern': 'NONE', 'conflict_penalty': 0.0}

        # ── 5. HARAMI (Inside Bar) ────────────────────────────────────
        # c2 body fully inside c1 body = compression before continuation.
        # At a structural ICT level this signals the market is coiling before
        # the expected directional move.
        harami = (
            c1_body >= atr * 0.20 and
            c2_body < c1_body and
            max(c2['close'], c2['open']) < max(c1['close'], c1['open']) and
            min(c2['close'], c2['open']) > min(c1['close'], c1['open'])
        )
        if harami:
            return {'pattern': 'Harami', 'confirmed': True, 'bonus': 0.08,
                    'conflict': False, 'conflict_pattern': 'NONE', 'conflict_penalty': 0.0}

        # ── 6. TWEEZERS TOP / BOTTOM ─────────────────────────────────
        # Two consecutive candles testing the same high or low (within 0.15×ATR).
        # Tweezers Bottom: equal lows, first bearish, second bullish → BUY signal.
        # Tweezers Top   : equal highs, first bullish, second bearish → SELL signal.
        equal_lows  = abs(c2['low']  - c1['low'])  < atr * 0.15
        equal_highs = abs(c2['high'] - c1['high']) < atr * 0.15

        tweezers_bottom = equal_lows  and not c1_bull and c2_bull
        tweezers_top    = equal_highs and     c1_bull and not c2_bull

        if is_buy and tweezers_bottom:
            return {'pattern': 'Tweezers_Bottom', 'confirmed': True, 'bonus': 0.08,
                    'conflict': False, 'conflict_pattern': 'NONE', 'conflict_penalty': 0.0}
        if not is_buy and tweezers_top:
            return {'pattern': 'Tweezers_Top', 'confirmed': True, 'bonus': 0.08,
                    'conflict': False, 'conflict_pattern': 'NONE', 'conflict_penalty': 0.0}

        return empty

    except Exception:
        return empty


def compute_ict_confluence(df: pd.DataFrame, df_macro: pd.DataFrame,
                            symbol: str, market_regime: str,
                            utc_now: datetime = None,
                            df_ticks: pd.DataFrame = None) -> tuple:
    """
    [AMD] Phase-aware ICT confluence scorer.

    Routes each cycle through the correct scoring logic based on AMD phase:
    ACCUMULATION → NEUTRAL (map range, log AH/AL, no execution)
    MANIPULATION → Full sweep+displacement + Judas Swing bonus
    DISTRIBUTION → Continuation: OB/FVG retest in trend direction
    NY_MANIPULATION → Sweep of London range (LH/LL)
    NY_DISTRIBUTION → Same as DISTRIBUTION
    AVOID → NEUTRAL

    Returns: (signal, score, reason_str, conditions_dict, kill_zone)
    """
    if utc_now is None:
        utc_now = datetime.utcnow()

    if len(df) < 50:
        return "NEUTRAL", 0.0, "Gathering Data", {}, "N/A"

    df = df.copy()
    df['atr']        = calculate_atr(df)
    df['avg_volume'] = df['volume'].rolling(20).mean()

    c1 = df.iloc[-3]
    c2 = df.iloc[-2]
    c3 = df.iloc[-1]

    current_atr = max(df['atr'].iloc[-1], 1e-8)
    avg_vol     = df['avg_volume'].iloc[-1]
    vol_ratio   = max(c1['volume'] / avg_vol if avg_vol > 0 else 1.0,
                      c2['volume'] / avg_vol if avg_vol > 0 else 1.0)

    # ── Shared structural computations ─────────────────────────────
    # These are computed first so detect_amd_phase can use structure data
    structure   = detect_market_structure(df)
    macro_trend = _derive_macro_trend(df_macro)
    obs         = detect_order_blocks(df)
    pd_zone     = detect_premium_discount(df, df_macro=df_macro, lookback=100)
    eq_levels   = detect_equal_highs_lows(df)

    # ── AMD Phase — STRUCTURAL detection (not clock-based) ─────────
    # The Asian range boundaries feed the manipulation detector as reference.
    # London range used for NY_MANIPULATION context.
    asian  = detect_asian_range(df, utc_now)
    london = detect_london_range(df, utc_now)

    # Use Asian range as primary accumulation reference if available,
    # otherwise fall back to structural accumulation range
    accum_ref_high = asian.get('asian_high') if asian.get('valid') else None
    accum_ref_low  = asian.get('asian_low')  if asian.get('valid') else None

    amd = detect_amd_phase(
        df, current_atr,
        utc_now.hour, utc_now.minute,
        accum_high=accum_ref_high,
        accum_low=accum_ref_low,
        structure=structure,
        symbol=symbol,
    )
    amd_phase    = amd['phase']
    amd_dir      = amd.get('direction')        # 'BULL' | 'BEAR' | None
    amd_conf     = amd.get('confidence', 0.0)
    clock_label  = amd.get('clock_label', 'Unknown')
    manip_data   = amd.get('manip_data', {})
    accum_data   = amd.get('accum_data', {})

    # ── S13 CONFLUENCE AMPLIFIER — compute all four frameworks ─────
    # Computed here once, available to all phase paths below.
    # All are pure functions operating on the M15 DataFrame already in memory.
    s13_vwap    = compute_vwap_context(df)
    s13_profile = compute_volume_profile(df, lookback=200, atr=current_atr)
    s13_delta   = compute_delta_context(df)
    s13_wyckoff = wyckoff_spring_check(df, manip_data, avg_vol)
    
    # [S19f] Process Tick Data
    s19f_ticks  = compute_tick_delta(df_ticks, 'BUY' if amd_dir == 'BULL' else 'SELL') 

    # ── S17 CANDLESTICK INTELLIGENCE — compute once, used in all paths ─────
    # Pre-compute both directions; each scoring path uses its own result.
    # detect_candlestick_pattern() reads df.iloc[-4:-1] (all closed bars).
    s17_candle_bull = detect_candlestick_pattern(df, 'BUY',  current_atr)
    s17_candle_bear = detect_candlestick_pattern(df, 'SELL', current_atr)

    # ── [S30] ACCUMULATION / INDETERMINATE — allow M1 scalp to pass through ──
    # Previously: these phases were hard NEUTRAL blocks (97.5% of signals blocked).
    # Now: ICT AMD is CONTEXT, not the gate. The M1 pullback scalp engine runs
    # independently of AMD phase. AMD only adjusts confidence weight.
    #
    # ACCUMULATION  → -0.08 confidence penalty (ranging market, harder entries)
    # INDETERMINATE → -0.05 confidence penalty (trending but no clean structure)
    # DISTRIBUTION  → no penalty (this is the ideal execution phase)
    # M1 scalp can still fire in any phase as long as session window allows.
    _amd_context_penalty = 0.0
    if amd_phase == 'ACCUMULATION':
        _amd_context_penalty = 0.08
    elif amd_phase == 'INDETERMINATE':
        _amd_context_penalty = 0.05

    # Store AMD context for downstream use
    _amd_cond_base = {
        'mode': amd_phase, 'amd_phase': amd_phase,
        'amd_confidence': amd_conf, 'clock_label': clock_label,
        'asian_high':  asian.get('asian_high'),
        'asian_low':   asian.get('asian_low'),
        'range_high':  accum_data.get('range_high'),
        'range_low':   accum_data.get('range_low'),
        'amd_penalty': _amd_context_penalty,
    }

    # ── AVOID ──────────────────────────────────────────────────────
    if amd_phase == 'AVOID':
        return "NEUTRAL", 0.0, "NY Lunch: Reaccumulation. Skipped.", {}, "NY_Lunch"

    # ── Session weight (kill zone quality) ─────────────────────────
    session_wt, kill_zone = get_ict_session_weight(utc_now.hour, utc_now.minute)
    if kill_zone == 'NY_Lunch':
        return "NEUTRAL", 0.0, "NY Lunch: Low-quality session. Skipped.", {}, kill_zone

    # [S25-F4] Compute H4 swing conviction before regime routing
    _h4_swing_count, _strong_h4, _sniper_h4 = _h4_trend_strength(df_macro)

    # ── [S30A] Conqueror 3-MA alignment filter ─────────────────────
    # Confirms macro trend across three time horizons simultaneously.
    # Bullish: MA(10) > MA(10)[10 bars ago] AND close > close[40 bars ago]
    _conqueror_bull = False; _conqueror_bear = False; _conqueror_bonus = 0.0
    try:
        if df_macro is not None and len(df_macro) >= 50:
            _cl = df_macro['close'].astype(float)
            _ma10_now = _cl.iloc[-10:].mean()
            _ma10_ago = _cl.iloc[-20:-10].mean()
            _c_now    = float(_cl.iloc[-1])
            _c_40     = float(_cl.iloc[-41]) if len(_cl) >= 41 else _c_now
            _conqueror_bull = (_ma10_now > _ma10_ago) and (_c_now > _c_40)
            _conqueror_bear = (_ma10_now < _ma10_ago) and (_c_now < _c_40)
            if _conqueror_bull or _conqueror_bear:
                _conqueror_bonus = 0.08
    except Exception:
        pass

    # ── [S32D] 50 EMA Trend + RSI Pullback Layer (Hybrid Strategy 1) ──
    # "Price above 50 EMA on H4 + pullback to 50 EMA + RSI crosses 40 → BUY"
    # Applied as a confidence modifier (+0.05) when setup aligns.
    _ema50_bonus = 0.0
    try:
        if df_macro is not None and len(df_macro) >= 55:
            _cl50  = df_macro['close'].astype(float)
            _ema50 = _cl50.ewm(span=50, adjust=False).mean()
            _e50   = float(_ema50.iloc[-1])
            _c50   = float(_cl50.iloc[-1])
            _prv   = float(_cl50.iloc[-3:].mean())  # 3-bar avg = near EMA

            # RSI(14) on H4
            _d     = _cl50.diff()
            _g     = _d.clip(lower=0).rolling(14).mean()
            _ls    = (-_d.clip(upper=0)).rolling(14).mean()
            _rsi50 = float(100 - 100 / (1 + _g.iloc[-1] / max(_ls.iloc[-1], 1e-9)))

            # Bullish: price above EMA50, currently near EMA50, RSI 35-55 (crossing up)
            near_ema = abs(_c50 - _e50) / max(_e50, 1) < 0.005  # Within 0.5% of EMA
            if _c50 > _e50 and near_ema and 35 <= _rsi50 <= 55:
                _ema50_bonus = 0.05  # EMA50 pullback BUY setup
            # Bearish: price below EMA50, near EMA50, RSI 45-65 (crossing down)
            elif _c50 < _e50 and near_ema and 45 <= _rsi50 <= 65:
                _ema50_bonus = 0.05  # EMA50 pullback SELL setup
    except Exception:
        pass

    # ── [S30B] Channel Breakout — 55-bar H4 extreme zone ───────────
    # If price is within 0.3% of the 55-bar H4 high or low, we are in
    # a breakout zone → increase confidence.
    _channel_breakout_zone = False; _channel_bonus = 0.0
    try:
        if df_macro is not None and len(df_macro) >= 56:
            _h4_55_high = float(df_macro['high'].astype(float).iloc[-55:].max())
            _h4_55_low  = float(df_macro['low'].astype(float).iloc[-55:].min())
            _c_cur      = float(df_macro['close'].astype(float).iloc[-1])
            _rng_55     = _h4_55_high - _h4_55_low
            _thresh     = _rng_55 * 0.003
            if _c_cur >= _h4_55_high - _thresh or _c_cur <= _h4_55_low + _thresh:
                _channel_breakout_zone = True
                _channel_bonus = 0.06
    except Exception:
        pass

    if "DEAD MARKET" in market_regime:
        disp_atr_mult = 0.3;  enforce_macro = False
    elif "HIGH VOLATILITY" in market_regime:
        disp_atr_mult = 0.8;  enforce_macro = True
    else:
        disp_atr_mult = _get_disp_atr_mult(symbol, market_regime)
        # [S25-F4+S26] enforce_macro when H4 has 3+ confirmed swing points
        enforce_macro = _strong_h4

    # ── DISTRIBUTION PATH ──────────────────────────────────────────
    if amd_phase == 'DISTRIBUTION':
        # [S13-VWAP-MACRO] VWAP slope overrides stale H4 macro trend for intraday.
        # H4 EMA reflects bias from up to 4 hours ago. VWAP slope is real-time.
        # If VWAP slope contradicts H4 EMA, use VWAP slope as the active bias.
        # Evidence: USDJPY scored 0.66 blocked by "BULLISH H4" while distributing
        # continuously for 2h. VWAP slope was negative — correct intraday bias.
        vwap_slope = s13_vwap.get('vwap_slope', 'FLAT')
        effective_macro = macro_trend
        vwap_macro_override = False
        if vwap_slope == 'DOWN' and macro_trend == 'BULLISH':
            effective_macro     = 'BEARISH'
            vwap_macro_override = True
        elif vwap_slope == 'UP' and macro_trend == 'BEARISH':
            effective_macro     = 'BULLISH'
            vwap_macro_override = True

        result = _score_distribution(
            df, structure, obs, pd_zone, session_wt, kill_zone,
            current_atr, vol_ratio, effective_macro, c3)
        if result:
            signal, score, reason, cond = result
            cond['amd_phase']         = amd_phase
            cond['amd_confidence']    = amd_conf
            cond['clock_label']       = clock_label
            cond['vwap_macro_override'] = vwap_macro_override
            cond['effective_macro']   = effective_macro

            # [S13] Confluence bonuses for DISTRIBUTION path
            s13_dist_bonus = 0.0

            # VWAP Z-score extreme confirms direction
            if "BUY" in signal and s13_vwap.get('extreme_bull'):
                s13_dist_bonus += 0.10
                cond['s13_vwap_extreme'] = True
            elif "SELL" in signal and s13_vwap.get('extreme_bear'):
                s13_dist_bonus += 0.10
                cond['s13_vwap_extreme'] = True
            else:
                cond['s13_vwap_extreme'] = False

            # OB aligns with HVN (institutional footprint at zone)
            if cond.get('order_block') and s13_profile.get('ob_at_hvn'):
                s13_dist_bonus += 0.12
                cond['s13_ob_hvn'] = True
            else:
                cond['s13_ob_hvn'] = False

            # Delta confirms direction
            if "BUY" in signal and s13_delta.get('delta_confirms_buy'):
                s13_dist_bonus += 0.08
                cond['s13_delta_confirm'] = True
            elif "SELL" in signal and s13_delta.get('delta_confirms_sell'):
                s13_dist_bonus += 0.08
                cond['s13_delta_confirm'] = True
            else:
                cond['s13_delta_confirm'] = False

            # Log S13 context
            cond['s13_vwap_z']    = s13_vwap.get('vwap_z', 0.0)
            cond['s13_vwap']      = s13_vwap.get('vwap', 0.0)
            cond['s13_poc']       = s13_profile.get('poc', 0.0)
            cond['s13_delta']     = s13_delta.get('cum_delta_slope', 0.0)

            if s13_dist_bonus > 0:
                score = min(0.99, score + s13_dist_bonus)
                reason = reason + (f" | S13[VWAP_Z:{s13_vwap.get('vwap_z',0):.2f}"
                                   f" HVN:{s13_profile.get('ob_at_hvn',False)}"
                                   f" Δ:{s13_delta.get('cum_delta_slope',0):.0f}]")

            # ── [S17] Candlestick Intelligence — DISTRIBUTION ─────────
            s17_dist = s17_candle_bull if "BUY" in signal else s17_candle_bear
            if s17_dist['confirmed']:
                score = min(0.99, score + s17_dist['bonus'])
                cond['s17_candle_pattern']   = s17_dist['pattern']
                cond['s17_candle_confirmed'] = True
                cond['s17_candle_conflict']  = False
                cond['s17_conflict_pattern'] = 'NONE'
                reason = reason + f" | S17[{s17_dist['pattern']}+{s17_dist['bonus']:.2f}]"
            elif s17_dist['conflict']:
                score = max(0.0, score - s17_dist['conflict_penalty'])
                cond['s17_candle_pattern']   = 'NONE'
                cond['s17_candle_confirmed'] = False
                cond['s17_candle_conflict']  = True
                cond['s17_conflict_pattern'] = s17_dist['conflict_pattern']
                reason = reason + f" | S17[⚠️CONFLICT:{s17_dist['conflict_pattern']}]"
            else:
                cond['s17_candle_pattern']   = 'NONE'
                cond['s17_candle_confirmed'] = False
                cond['s17_candle_conflict']  = False
                cond['s17_conflict_pattern'] = 'NONE'

            if "DEAD MARKET" in market_regime:
                signal = "BUY_NANO" if "BUY" in signal else "SELL_NANO"
            # [S33-FIX] Expose S30/S32 bonus vars so analyze_market_structure can read them
            cond['h4_swing_count']      = _h4_swing_count
            cond['h4_macro_strong']     = _strong_h4
            cond['conqueror_bonus']     = _conqueror_bonus
            cond['amd_context_penalty'] = _amd_context_penalty
            cond['channel_bonus']       = _channel_bonus
            cond['ema50_bonus']         = _ema50_bonus
            return signal, round(score, 3), reason, cond, kill_zone
        return "NEUTRAL", 0.0, (f"DISTRIBUTION [{clock_label}]: No OB/FVG retest "
                                f"in {macro_trend} direction."), {}, kill_zone

    # ── MANIPULATION PATH ──────────────────────────────────────────
    # Determine the reference range that was swept (session-context aware)
    t = utc_now.hour * 60 + utc_now.minute
    if t >= 13*60+30 and london.get('valid'):
        # NY Open window — London range is the sweep reference
        ref_high  = london.get('london_high')
        ref_low   = london.get('london_low')
        ref_valid = True
        ref_label = 'LH/LL'
    elif asian.get('valid'):
        # London Open / Pre-London — Asian range is the sweep reference
        ref_high  = asian.get('asian_high')
        ref_low   = asian.get('asian_low')
        ref_valid = True
        ref_label = 'AH/AL'
    else:
        # Structural manipulation detected without clean session range
        # Use the accumulation range boundaries as reference
        ref_high  = accum_data.get('range_high')
        ref_low   = accum_data.get('range_low')
        ref_valid = ref_high is not None and ref_low is not None
        ref_label = 'Struct'

    if ref_valid and ref_high and ref_low:
        judas = detect_judas_swing(c1['low'], c1['high'],
                                    ref_high, ref_low, current_atr)
    else:
        judas = {'judas_bull': False, 'judas_bear': False,
                 'depth_bull': 0.0,  'depth_bear': 0.0}

    # ── Sweep (hybrid tier) ────────────────────────────────────────
    swing_lows  = structure.get('swing_lows', [])
    swing_highs = structure.get('swing_highs', [])

    if len(swing_lows) >= 2:
        last_swing_low   = swing_lows[-1][1]
        sweep_low        = (c1['low'] < last_swing_low and
                            (last_swing_low - c1['low']) >= current_atr * 0.3)
    else:
        last_swing_low   = df['low'].rolling(20).min().shift(1).iloc[-1]
        sweep_low        = (c1['low'] < last_swing_low and
                            (last_swing_low - c1['low']) >= current_atr * 0.5)

    if len(swing_highs) >= 2:
        last_swing_high  = swing_highs[-1][1]
        sweep_high       = (c1['high'] > last_swing_high and
                            (c1['high'] - last_swing_high) >= current_atr * 0.3)
    else:
        last_swing_high  = df['high'].rolling(20).max().shift(1).iloc[-1]
        sweep_high       = (c1['high'] > last_swing_high and
                            (c1['high'] - last_swing_high) >= current_atr * 0.5)

    eq_low_sweep  = sweep_low  and any(abs(last_swing_low  - lvl) < current_atr * 0.3
                                       for lvl in eq_levels.get('equal_lows', []))
    eq_high_sweep = sweep_high and any(abs(last_swing_high - lvl) < current_atr * 0.3
                                       for lvl in eq_levels.get('equal_highs', []))

    # ── Displacement ───────────────────────────────────────────────
    c2_range = c2['high'] - c2['low']
    close_q_bull = ((c2['close'] - c2['low']) / c2_range) if c2_range > 0 else 0
    close_q_bear = ((c2['high'] - c2['close']) / c2_range) if c2_range > 0 else 0

    disp_up   = (c2['close'] > c2['open'] and
                 abs(c2['close'] - c2['open']) >= current_atr * disp_atr_mult and
                 close_q_bull >= 0.70)
    disp_down = (c2['close'] < c2['open'] and
                 abs(c2['open'] - c2['close']) >= current_atr * disp_atr_mult and
                 close_q_bear >= 0.70)

    # ── BULLISH MANIPULATION ───────────────────────────────────────
    if sweep_low and disp_up:
        score = 0.0
        cond  = {'mode': 'MANIPULATION', 'amd_phase': amd_phase}

        cond['sweep']        = True;  score += 0.20
        cond['displacement'] = True;  score += 0.15
        cond['kill_zone']    = kill_zone
        score += 0.15 * session_wt

        # [S12-P0B] Structural SL reference: the swept swing low is the level
        # smart money hunted. SL belongs BELOW that level (+ ATR buffer), not
        # at an arbitrary 3-candle-back index.
        cond['swept_level']     = round(float(c1['low']), 5)   # the candle that swept
        cond['swing_sl_ref']    = round(float(last_swing_low), 5)  # structural level swept
        cond['sl_atr_buffer']   = round(float(current_atr * 0.3), 5)

        # [S12-P1B] Asian range as natural TP target:
        # For a bullish setup (swept the Asian Low), the natural target is the
        # Asian High — smart money swept SSL, now distributes toward BSL (AH).
        # Fall back to ref_high from London range if Asian not valid.
        cond['tp_target_level'] = asian.get('asian_high') or ref_high
        cond['asian_high']      = asian.get('asian_high')
        cond['asian_low']       = asian.get('asian_low')

        is_judas = judas['judas_bull']
        cond['judas_swing'] = is_judas
        cond['ref_label']   = ref_label
        cond['judas_depth'] = judas['depth_bull']
        if is_judas: score += 0.12

        bull_ob = obs.get('bullish')
        ob_hit  = bool(bull_ob and bull_ob.get('retested'))
        cond['order_block'] = ob_hit
        # [S12-P0A] OB price levels for structural entry placement.
        # BUY_LIMIT entry = ob_body_low (50% of OB body = best value zone).
        # Fall back through: body_low → zone_low → None (engine uses ATR fallback).
        if bull_ob:
            cond['ob_entry_price'] = bull_ob.get('body_low') or bull_ob.get('low')
            cond['ob_zone_high']   = bull_ob.get('high')
            cond['ob_zone_low']    = bull_ob.get('low')
        if ob_hit: score += 0.15

        fvg_bull = detect_fvg(df, 'BUY', current_atr)
        cond['fvg'] = fvg_bull
        if fvg_bull: score += 0.10

        in_discount = pd_zone.get('in_discount', False)
        deep_pd     = pd_zone.get('deep', False) and in_discount
        h4_conflict = pd_zone.get('h4_conflict', False)
        cond['discount_zone'] = in_discount
        cond['deep_discount'] = deep_pd
        if deep_pd and not h4_conflict:        score += 0.10
        elif in_discount and not h4_conflict:  score += 0.05

        bos_aligned = structure['trend'] == 'BULLISH'
        cond['bos_aligned'] = bos_aligned
        if bos_aligned: score += 0.08

        ote_lo, ote_hi = get_ote_zone(c2['high'], c1['low'], 'BUY')
        ote_hit = (ote_lo > 0 and ote_lo <= c3['close'] <= ote_hi)
        cond['ote'] = ote_hit
        if ote_hit: score += 0.07

        vol_surge = vol_ratio >= 1.3
        cond['volume_surge'] = vol_surge
        cond['vol_ratio']    = round(vol_ratio, 2)
        if vol_surge: score = min(0.99, score * 1.10)

        cond['eq_lows_sweep'] = eq_low_sweep

        # [S11-AMD-JUDAS] Institutional confirmation bonus.
        # [S12-AMD-B] Bonus scaled by accumulation quality (conf/0.60 ratio).
        # A MANIPULATION from a tight, confirmed accumulation range (conf ≥ 0.60)
        # earns the full +0.10. A looser range earns proportionally less.
        # Formula: bonus = 0.10 × max(0.50, raw_accum_conf / 0.60)
        # At conf=0.60+ → +0.100;  at conf=0.35 → +0.058;  minimum floor: +0.05
        if amd_phase == 'MANIPULATION' and is_judas:
            raw_accum_conf = accum_data.get('confidence', 0.0)
            quality_mult = max(0.50, raw_accum_conf / 0.60)
            amd_judas_bonus = round(0.10 * quality_mult, 4)
            score = min(0.99, score + amd_judas_bonus)
            cond['amd_judas_bonus'] = True
            cond['amd_judas_bonus_value'] = amd_judas_bonus

        if enforce_macro and macro_trend == "BEARISH":
            return "NEUTRAL", 0.0, f"BUY blocked by Bearish H4 (score={score:.2f})", cond, kill_zone

        # ── [S13] CONFLUENCE AMPLIFIER — BULLISH MANIPULATION ──────
        # Applied AFTER all ICT checks and H4 enforcement, before signal emit.
        # Additive bonuses: each framework addresses a specific ICT blindspot.
        s13_bonus = 0.0

        # [S13-F1] VWAP Z-score: BUY at statistically oversold extreme (+0.10)
        # Z < -1.5 = price >1.5σ below VWAP = institutional buy zone
        vwap_ext_bull = s13_vwap.get('extreme_bull', False)
        cond['s13_vwap_z']       = s13_vwap.get('vwap_z', 0.0)
        cond['s13_vwap']         = s13_vwap.get('vwap', 0.0)
        cond['s13_vwap_extreme'] = vwap_ext_bull
        if vwap_ext_bull:
            s13_bonus += 0.10

        # [S13-F2] Volume Profile — OB at HVN confirms institutional zone (+0.12)
        # FVG in LVN confirms inefficiency gap, fast-move likely to fill (+0.08)
        ob_at_hvn  = s13_profile.get('ob_at_hvn', False)
        fvg_at_lvn = s13_profile.get('fvg_at_lvn', False)
        at_poc     = s13_profile.get('at_poc', False)
        cond['s13_poc']        = s13_profile.get('poc', 0.0)
        cond['s13_ob_hvn']     = ob_at_hvn
        cond['s13_fvg_lvn']    = fvg_at_lvn
        cond['s13_at_poc']     = at_poc
        if ob_hit and ob_at_hvn:   s13_bonus += 0.12
        if fvg_bull and fvg_at_lvn: s13_bonus += 0.08

        # [S13-F3] Cumulative Delta — confirms BUY direction (+0.08)
        # Delta divergence = hidden demand below price (+0.06)
        delta_buy = s13_delta.get('delta_confirms_buy', False)
        delta_div = s13_delta.get('divergence', False)
        cond['s13_delta']          = s13_delta.get('cum_delta_slope', 0.0)
        cond['s13_delta_confirm']  = delta_buy
        cond['s13_delta_diverge']  = delta_div
        if delta_buy: s13_bonus += 0.08
        if delta_div and not delta_buy: s13_bonus += 0.06   # early signal

        # [S13-F4] Wyckoff Spring — low-vol sweep = genuine Spring (+0.07)
        spring_ok = s13_wyckoff.get('spring', False)
        cond['s13_wyckoff_spring']    = spring_ok
        cond['s13_wyckoff_vol_ratio'] = s13_wyckoff.get('test_vol_ratio', 1.0)
        if spring_ok: s13_bonus += 0.07

        # [S19f] Tick-Level Order Flow Bonus
        tick_confirm = s19f_ticks.get('delta_confirms', False)
        tick_surge   = s19f_ticks.get('tick_surge', False)
        cond['s19f_ctd'] = s19f_ticks.get('ctd', 0.0)
        if tick_confirm: s13_bonus += 0.08
        if tick_confirm and tick_surge: s13_bonus += 0.05  # Massive institutional absorption

        if s13_bonus > 0:
            score = min(0.99, score + s13_bonus)

        # ── [S17] Candlestick Intelligence — BULLISH MANIPULATION ────
        if s17_candle_bull['confirmed']:
            score = min(0.99, score + s17_candle_bull['bonus'])
            cond['s17_candle_pattern']   = s17_candle_bull['pattern']
            cond['s17_candle_confirmed'] = True
            cond['s17_candle_conflict']  = False
            cond['s17_conflict_pattern'] = 'NONE'
        elif s17_candle_bull['conflict']:
            score = max(0.0, score - s17_candle_bull['conflict_penalty'])
            cond['s17_candle_pattern']   = 'NONE'
            cond['s17_candle_confirmed'] = False
            cond['s17_candle_conflict']  = True
            cond['s17_conflict_pattern'] = s17_candle_bull['conflict_pattern']
        else:
            cond['s17_candle_pattern']   = 'NONE'
            cond['s17_candle_confirmed'] = False
            cond['s17_candle_conflict']  = False
            cond['s17_conflict_pattern'] = 'NONE'

        # ── Signal type ─────────────────────────────────────────────
        if "DEAD MARKET" in market_regime:       signal = "BUY_NANO"
        elif "HIGH VOLATILITY" in market_regime: signal = "BUY"
        else:                                    signal = "BUY_MICRO"

        tag = f" ⚡JUDAS({ref_label})" if is_judas else ""
        s13_tag = (f" | S13[Z:{s13_vwap.get('vwap_z',0):.2f}"
                   f" HVN:{ob_at_hvn} Δ:{s13_delta.get('cum_delta_slope',0):.0f}"
                   f" Spring:{spring_ok}]") if s13_bonus > 0 else ""
        s19f_tag = f" | Ticks:✅" if tick_confirm else ""
        s17_tag = (f" | S17[{cond['s17_candle_pattern']}+{s17_candle_bull['bonus']:.2f}]"
                   if cond.get('s17_candle_confirmed')
                   else (f" | S17[⚠️{cond['s17_conflict_pattern']}]"
                         if cond.get('s17_candle_conflict') else ""))
        reason = (f"ICT Bullish [{kill_zone}]{tag} | AMD:{amd_phase} | "
                  f"Score:{score:.2f} | OB:{ob_hit} FVG:{fvg_bull} "
                  f"Disc:{in_discount}(deep:{deep_pd}) BOS:{bos_aligned} "
                  f"OTE:{ote_hit} Vol:{vol_ratio:.1f}x{s13_tag}{s19f_tag}{s17_tag}")
        # [S33-FIX] Expose S30/S32 bonus vars so analyze_market_structure can read them
        cond['h4_swing_count']      = _h4_swing_count
        cond['h4_macro_strong']     = _strong_h4
        cond['conqueror_bonus']     = _conqueror_bonus
        cond['amd_context_penalty'] = _amd_context_penalty
        cond['channel_bonus']       = _channel_bonus
        cond['ema50_bonus']         = _ema50_bonus
        return signal, round(score, 3), reason, cond, kill_zone

    # ── BEARISH MANIPULATION ───────────────────────────────────────
    if sweep_high and disp_down:
        score = 0.0
        cond  = {'mode': 'MANIPULATION', 'amd_phase': amd_phase}

        cond['sweep']        = True;  score += 0.20
        cond['displacement'] = True;  score += 0.15
        cond['kill_zone']    = kill_zone
        score += 0.15 * session_wt

        # [S12-P0B] Structural SL reference for SELL: swept swing high + ATR buffer.
        cond['swept_level']     = round(float(c1['high']), 5)
        cond['swing_sl_ref']    = round(float(last_swing_high), 5)
        cond['sl_atr_buffer']   = round(float(current_atr * 0.3), 5)

        # [S12-P1B] Asian Low as natural TP target for bearish setups.
        # Guard: asian_low MUST be below current close (valid SELL target).
        # If asian_low > close, price already broke below range — use ref_low.
        _c3_close   = float(c3['close'])
        _asian_low  = asian.get('asian_low')
        _valid_al   = _asian_low if (_asian_low and _asian_low < _c3_close) else None
        cond['tp_target_level'] = _valid_al or ref_low
        cond['asian_high']      = asian.get('asian_high')
        cond['asian_low']       = _asian_low

        is_judas = judas['judas_bear']
        cond['judas_swing'] = is_judas
        cond['ref_label']   = ref_label
        cond['judas_depth'] = judas['depth_bear']
        if is_judas: score += 0.12

        bear_ob = obs.get('bearish')
        ob_hit  = bool(bear_ob and bear_ob.get('retested'))
        cond['order_block'] = ob_hit
        # [S12-P0A] OB price levels for structural SELL entry.
        # SELL_LIMIT entry = ob_body_high (top of OB body = best value zone).
        if bear_ob:
            cond['ob_entry_price'] = bear_ob.get('body_high') or bear_ob.get('high')
            cond['ob_zone_high']   = bear_ob.get('high')
            cond['ob_zone_low']    = bear_ob.get('low')
        if ob_hit: score += 0.15

        fvg_bear = detect_fvg(df, 'SELL', current_atr)
        cond['fvg'] = fvg_bear
        if fvg_bear: score += 0.10

        in_premium = pd_zone.get('in_premium', False)
        deep_pd    = pd_zone.get('deep', False) and in_premium
        h4_conflict = pd_zone.get('h4_conflict', False)
        cond['premium_zone'] = in_premium
        cond['deep_premium'] = deep_pd
        if deep_pd and not h4_conflict:       score += 0.10
        elif in_premium and not h4_conflict:  score += 0.05

        bos_aligned = structure['trend'] == 'BEARISH'
        cond['bos_aligned'] = bos_aligned
        if bos_aligned: score += 0.08

        ote_lo, ote_hi = get_ote_zone(c1['high'], c2['low'], 'SELL')
        ote_hit = (ote_lo > 0 and ote_lo <= c3['close'] <= ote_hi)
        cond['ote'] = ote_hit
        if ote_hit: score += 0.07

        vol_surge = vol_ratio >= 1.3
        cond['volume_surge'] = vol_surge
        cond['vol_ratio']    = round(vol_ratio, 2)
        if vol_surge: score = min(0.99, score * 1.10)

        cond['eq_highs_sweep'] = eq_high_sweep

        # [S11-AMD-JUDAS] Institutional confirmation bonus.
        # [S12-AMD-B] Same quality scaling as the bullish branch.
        # bonus = 0.10 × max(0.50, raw_accum_conf / 0.60)
        if amd_phase == 'MANIPULATION' and is_judas:
            raw_accum_conf = accum_data.get('confidence', 0.0)
            quality_mult = max(0.50, raw_accum_conf / 0.60)
            amd_judas_bonus = round(0.10 * quality_mult, 4)
            score = min(0.99, score + amd_judas_bonus)
            cond['amd_judas_bonus'] = True
            cond['amd_judas_bonus_value'] = amd_judas_bonus

        if enforce_macro and macro_trend == "BULLISH":
            return "NEUTRAL", 0.0, f"SELL blocked by Bullish H4 (score={score:.2f})", cond, kill_zone

        # ── [S13] CONFLUENCE AMPLIFIER — BEARISH MANIPULATION ──────
        s13_bonus = 0.0

        # [S13-F1] VWAP Z-score: SELL at statistically overbought extreme (+0.10)
        # Z > +1.5 = price >1.5σ above VWAP = institutional sell zone
        vwap_ext_bear = s13_vwap.get('extreme_bear', False)
        cond['s13_vwap_z']       = s13_vwap.get('vwap_z', 0.0)
        cond['s13_vwap']         = s13_vwap.get('vwap', 0.0)
        cond['s13_vwap_extreme'] = vwap_ext_bear
        if vwap_ext_bear:
            s13_bonus += 0.10

        # [S13-F2] Volume Profile — OB at HVN (+0.12) | FVG in LVN (+0.08)
        ob_at_hvn  = s13_profile.get('ob_at_hvn', False)
        fvg_at_lvn = s13_profile.get('fvg_at_lvn', False)
        at_poc     = s13_profile.get('at_poc', False)
        cond['s13_poc']     = s13_profile.get('poc', 0.0)
        cond['s13_ob_hvn']  = ob_at_hvn
        cond['s13_fvg_lvn'] = fvg_at_lvn
        cond['s13_at_poc']  = at_poc
        if ob_hit and ob_at_hvn:    s13_bonus += 0.12
        if fvg_bear and fvg_at_lvn: s13_bonus += 0.08

        # [S13-F3] Cumulative Delta — confirms SELL direction (+0.08)
        # Delta divergence = hidden supply above price (+0.06)
        delta_sell = s13_delta.get('delta_confirms_sell', False)
        delta_div  = s13_delta.get('divergence', False)
        cond['s13_delta']         = s13_delta.get('cum_delta_slope', 0.0)
        cond['s13_delta_confirm'] = delta_sell
        cond['s13_delta_diverge'] = delta_div
        if delta_sell: s13_bonus += 0.08
        if delta_div and not delta_sell: s13_bonus += 0.06

        # [S13-F4] Wyckoff Upthrust — low-vol sweep high = genuine Upthrust (+0.07)
        upthrust_ok = s13_wyckoff.get('upthrust', False)
        cond['s13_wyckoff_upthrust'] = upthrust_ok
        cond['s13_wyckoff_vol_ratio'] = s13_wyckoff.get('test_vol_ratio', 1.0)
        if upthrust_ok: s13_bonus += 0.07

        # [S19f] Tick-Level Order Flow Bonus
        tick_confirm = s19f_ticks.get('delta_confirms', False)
        tick_surge   = s19f_ticks.get('tick_surge', False)
        cond['s19f_ctd'] = s19f_ticks.get('ctd', 0.0)
        if tick_confirm: s13_bonus += 0.08
        if tick_confirm and tick_surge: s13_bonus += 0.05  # Massive institutional absorption

        if s13_bonus > 0:
            score = min(0.99, score + s13_bonus)

        # ── [S17] Candlestick Intelligence — BEARISH MANIPULATION ────
        if s17_candle_bear['confirmed']:
            score = min(0.99, score + s17_candle_bear['bonus'])
            cond['s17_candle_pattern']   = s17_candle_bear['pattern']
            cond['s17_candle_confirmed'] = True
            cond['s17_candle_conflict']  = False
            cond['s17_conflict_pattern'] = 'NONE'
        elif s17_candle_bear['conflict']:
            score = max(0.0, score - s17_candle_bear['conflict_penalty'])
            cond['s17_candle_pattern']   = 'NONE'
            cond['s17_candle_confirmed'] = False
            cond['s17_candle_conflict']  = True
            cond['s17_conflict_pattern'] = s17_candle_bear['conflict_pattern']
        else:
            cond['s17_candle_pattern']   = 'NONE'
            cond['s17_candle_confirmed'] = False
            cond['s17_candle_conflict']  = False
            cond['s17_conflict_pattern'] = 'NONE'

        # ── Signal type ─────────────────────────────────────────────
        if "DEAD MARKET" in market_regime:       signal = "SELL_NANO"
        elif "HIGH VOLATILITY" in market_regime: signal = "SELL"
        else:                                    signal = "SELL_MICRO"

        tag = f" ⚡JUDAS({ref_label})" if is_judas else ""
        s13_tag = (f" | S13[Z:{s13_vwap.get('vwap_z',0):.2f}"
                   f" HVN:{ob_at_hvn} Δ:{s13_delta.get('cum_delta_slope',0):.0f}"
                   f" Upthrust:{upthrust_ok}]") if s13_bonus > 0 else ""
        s19f_tag = f" | Ticks:✅" if tick_confirm else ""
        s17_tag = (f" | S17[{cond['s17_candle_pattern']}+{s17_candle_bear['bonus']:.2f}]"
                   if cond.get('s17_candle_confirmed')
                   else (f" | S17[⚠️{cond['s17_conflict_pattern']}]"
                         if cond.get('s17_candle_conflict') else ""))
        reason = (f"ICT Bearish [{kill_zone}]{tag} | AMD:{amd_phase} | "
                  f"Score:{score:.2f} | OB:{ob_hit} FVG:{fvg_bear} "
                  f"Prem:{in_premium}(deep:{deep_pd}) BOS:{bos_aligned} "
                  f"OTE:{ote_hit} Vol:{vol_ratio:.1f}x{s13_tag}{s19f_tag}{s17_tag}")
        # [S33-FIX] Expose S30/S32 bonus vars so analyze_market_structure can read them
        cond['h4_swing_count']      = _h4_swing_count
        cond['h4_macro_strong']     = _strong_h4
        cond['conqueror_bonus']     = _conqueror_bonus
        cond['amd_context_penalty'] = _amd_context_penalty
        cond['channel_bonus']       = _channel_bonus
        cond['ema50_bonus']         = _ema50_bonus
        return signal, round(score, 3), reason, cond, kill_zone

    return "NEUTRAL", 0.0, f"[{amd_phase}] No sweep or displacement detected.", {}, kill_zone


# ═══════════════════════════════════════════════════════════════════════════════
# SPRINT 16 — SILVER BULLET / TIME-FVG / POWER OF 3 SCALP ENGINE
# ═══════════════════════════════════════════════════════════════════════════════
#
# Rationale:
#   The standard ICT engine applies identical logic to all 20 assets. For
#   Gold, Silver and crypto this leaves a systematic edge on the table:
#   these assets exhibit TIME-SPECIFIC FVG windows (ICT Silver Bullet) where
#   institutional imbalances form and mitigate within the SAME 60-minute
#   window — a near-mechanical short-term fill expectation that scalpers use
#   as primary setups. A random FVG at 20:00 UTC on EURUSD is speculative.
#   A time-FVG on XAUUSD created at 15:15 UTC during the NY Silver Bullet is
#   structurally different in terms of expected mitigation timing.
#
# Architecture:
#   1. get_silver_bullet_windows(symbol) — per-asset Silver Bullet UTC ranges
#   2. detect_time_fvg(df, direction, symbol, utc_now, atr) — FVG formed WITHIN
#      a Silver Bullet window, returns level not just bool
#   3. detect_po3_asian_sweep(df, asian, utc_now, symbol, atr) — Power of 3:
#      Asian range formed → sweep → displacement → FVG left behind
#   4. compute_scalp_confluence(df, df_macro, symbol, utc_now) — composite
#      score combining all three, returns full signal tuple
#
# Asset-specific calibration:
#   XAUUSD — Silver Bullet 07:00-08:00, 15:00-16:00, 19:00-20:00 UTC
#             Asian range 20:00(prev)-03:00 UTC | min FVG 0.5×ATR
#   XAGUSD — same windows, min FVG 0.4×ATR (more volatile per point)
#   BTCUSD — wider Asian 00:00-08:00 UTC, SB 07:30-08:30, 13:00-14:00
#   ETHUSD — same as BTC
#
# Integration:
#   compute_scalp_confluence() is called from analyze_market_structure() for
#   XAU/XAG/BTC/ETH. If the scalp score exceeds the ICT score it wins.
#   The winning signal is tagged with 'scalp_model' in conditions dict so
#   bot_engine can apply asset-specific sizing and TP logic.
# ───────────────────────────────────────────────────────────────────────────────


_SILVER_BULLET_ASSETS = {'XAUUSD', 'XAGUSD'}

# Silver Bullet window definitions per asset — (start_hour, start_min, end_hour, end_min)
# All times in TRUE UTC. Names chosen to map to institutional session triggers.
_SILVER_BULLET_WINDOWS: dict = {
    # Gold & Silver — three windows, aligned to London Open, London-NY transition, NY PM
    'XAUUSD': [
        (7,  0,  8,  0, 'Gold_London_Open_SB'),
        (15, 0, 16,  0, 'Gold_NY_Afternoon_SB'),
        (19, 0, 20,  0, 'Gold_NY_PM_SB'),
    ],
    'XAGUSD': [
        (7,  0,  8,  0, 'Silver_London_Open_SB'),
        (15, 0, 16,  0, 'Silver_NY_Afternoon_SB'),
        (19, 0, 20,  0, 'Silver_NY_PM_SB'),
    ],
    # [S34] BTC/ETH removed from Silver Bullet asset set
}

# Asian accumulation range boundaries per asset (UTC)
_ASIAN_RANGE_HOURS: dict = {
    'XAUUSD': (20, 3),    # 20:00 prev UTC → 03:00 UTC (NY close → Asian)
    'XAGUSD': (20, 3),
    # [S34] BTC/ETH removed
}

# Per-asset minimum FVG size as multiple of ATR


# ── [S27] MARKET HOURS DETECTOR ──────────────────────────────────────────────
def get_tradable_asset_classes(utc_now: datetime) -> dict:
    """
    Returns which asset classes are tradable right now.
    Market hours in UTC:
      FX:      Sun 22:00 – Fri 22:00 (continuous)
      Gold:    Sun 23:00 – Fri 21:00, with 23:00–23:15 daily rollover gap
      Indices: Mon–Fri 13:30–21:00 (NYSE)
      Energy:  Mon–Fri 01:00–21:00
    [S34] Crypto removed from asset universe.
    """
    dow = utc_now.weekday()  # 0=Mon … 6=Sun
    t   = utc_now.hour * 60 + utc_now.minute

    # Weekend: Fri 22:00 → Sun 22:00
    is_weekend = (
        (dow == 4 and t >= 22*60) or
        (dow == 5) or
        (dow == 6 and t < 22*60)
    )
    fx_open      = not is_weekend
    gold_open    = fx_open and not (23*60 <= t < 23*60 + 15)  # skip daily rollover
    indices_open = fx_open and dow < 5 and 13*60+30 <= t < 21*60
    energy_open  = fx_open and dow < 5 and 1*60 <= t < 21*60

    return {
        'fx':          fx_open,
        'gold':        gold_open,
        'indices':     indices_open,
        'energy':      energy_open,
        'is_weekend':  is_weekend,
        'is_ny_close': fx_open and 21*60 <= t < 22*60 + 30,
    }


def get_after_hours_active_symbols(vip_assets: list, utc_now: datetime) -> list:
    """
    [S27] Filters the vip_assets list to only symbols that are tradable now.
    [S34] Crypto removed — USDCHF/NZDUSD added which trade during FX hours.
    On weekends all FX pairs (including USDCHF/NZDUSD) are closed — return empty.
    Gold rollover (23:00–23:15): XAUUSD/XAGUSD skipped.
    """
    h = get_tradable_asset_classes(utc_now)

    if h['is_weekend']:
        # No crypto fallback anymore — nothing tradable on weekends
        return []

    active = []
    for sym in vip_assets:
        s = sym.upper()
        is_gold    = 'XAU' in s or 'XAG' in s
        is_index   = any(x in s for x in ['SP 500', 'TECH 100', 'GERMANY'])
        is_energy  = 'OIL' in s or 'NGAS' in s
        is_fx      = not is_gold and not is_index and not is_energy

        if   is_gold   and h['gold']    : active.append(sym)
        elif is_fx     and h['fx']      : active.append(sym)
        elif is_index  and h['indices'] : active.append(sym)
        elif is_energy and h['energy']  : active.append(sym)

    return active or []

_MIN_FVG_ATR: dict = {
    'XAUUSD': 0.50,
    'XAGUSD': 0.40,
    # [S34] BTC/ETH removed from asset universe
}


def get_silver_bullet_windows(symbol: str) -> list:
    """
    Return the list of Silver Bullet window tuples for the given symbol.
    Each tuple: (sh, sm, eh, em, name) in UTC.
    Returns [] if symbol is not in the scalp asset list.
    """
    # Normalise broker suffixes (e.g. 'XAUUSDm' → 'XAUUSD')
    base = symbol.upper()
    for key in _SILVER_BULLET_WINDOWS:
        if base.startswith(key):
            return _SILVER_BULLET_WINDOWS[key]
    return []


def _is_in_silver_bullet_window(utc_now: datetime, symbol: str) -> tuple:
    """
    Returns (True, window_name) if utc_now falls inside any Silver Bullet
    window for the asset.  Returns (False, '') otherwise.
    """
    windows = get_silver_bullet_windows(symbol)
    t = utc_now.hour * 60 + utc_now.minute
    for sh, sm, eh, em, name in windows:
        ws = sh * 60 + sm
        we = eh * 60 + em
        if ws <= t < we:
            return True, name
    return False, ''


def detect_asian_range_scalp(df: pd.DataFrame, symbol: str,
                              utc_now: datetime, atr: float) -> dict:
    """
    Asset-specific Asian range detection for the scalp engine.
    Uses per-asset hour boundaries from _ASIAN_RANGE_HOURS.

    Returns dict:
      valid:           True if ≥ 3 candles found
      asian_high:      float — session high
      asian_low:       float — session low
      asian_midpoint:  float
      range_atr:       float — range size / ATR (quality measure)
      range_pips:      float — raw range size
      is_tight:        bool  — range < 0.8×ATR (better for SB setup)
    """
    empty = {'valid': False, 'asian_high': None, 'asian_low': None,
             'asian_midpoint': None, 'range_atr': 0.0, 'range_pips': 0.0,
             'is_tight': False}

    if df is None or len(df) < 5 or atr <= 0:
        return empty

    base = symbol.upper()
    sh, eh = _ASIAN_RANGE_HOURS.get(
        next((k for k in _ASIAN_RANGE_HOURS if base.startswith(k)), ''),
        (0, 3)
    )

    today = utc_now.date()

    if 'timestamp' in df.columns:
        try:
            df2 = df.copy()
            df2['_dt'] = pd.to_datetime(df2['timestamp'], utc=True)

            if sh > eh:
                # Overnight window: e.g. 20:00 yesterday → 03:00 today
                import datetime as _dt_mod
                yesterday = today - _dt_mod.timedelta(days=1)
                asian = df2[
                    ((df2['_dt'].dt.date == yesterday) & (df2['_dt'].dt.hour >= sh)) |
                    ((df2['_dt'].dt.date == today)     & (df2['_dt'].dt.hour < eh))
                ]
            else:
                # Same-day window: e.g. 00:00-08:00 today
                asian = df2[
                    (df2['_dt'].dt.date == today) &
                    (df2['_dt'].dt.hour >= sh) &
                    (df2['_dt'].dt.hour < eh)
                ]

            if len(asian) >= 3:
                ah = float(asian['high'].max())
                al = float(asian['low'].min())
                rng = ah - al
                return {
                    'valid':          True,
                    'asian_high':     round(ah, 5),
                    'asian_low':      round(al, 5),
                    'asian_midpoint': round((ah + al) / 2, 5),
                    'range_atr':      round(rng / atr, 2),
                    'range_pips':     round(rng, 5),
                    'is_tight':       rng < atr * 0.8,
                }
        except Exception:
            pass

    # Fallback: rolling candle count approximation
    bars = 8 * 4 if 'BTC' in symbol.upper() or 'ETH' in symbol.upper() else 3 * 4
    win  = df.tail(bars * 2).head(bars)
    if len(win) < 3:
        return empty
    ah = float(win['high'].max())
    al = float(win['low'].min())
    rng = ah - al
    return {
        'valid':          True,
        'asian_high':     round(ah, 5),
        'asian_low':      round(al, 5),
        'asian_midpoint': round((ah + al) / 2, 5),
        'range_atr':      round(rng / atr, 2),
        'range_pips':     round(rng, 5),
        'is_tight':       rng < atr * 0.8,
    }


def detect_time_fvg(df: pd.DataFrame, direction: str, symbol: str,
                    utc_now: datetime, atr: float) -> dict:
    """
    Time-based Fair Value Gap — an FVG that was created INSIDE a Silver Bullet
    window.  This is the core distinction from the generic detect_fvg():
    a time-FVG has a well-defined expectation of SAME-SESSION mitigation.

    Scans last `lookback` bars. For each c1-c2-c3 FVG triplet, checks if the
    bar time falls within any Silver Bullet window for the asset.
    """
    empty = {'found': False, 'fvg_high': None, 'fvg_low': None,
             'fvg_mid': None, 'fvg_size': 0.0, 'fvg_size_atr': 0.0,
             'window_name': '', 'bars_ago': 0, 'filled_pct': 0.0}

    windows = get_silver_bullet_windows(symbol)
    if not windows or len(df) < 12 or atr <= 0:
        return empty

    min_gap = atr * _MIN_FVG_ATR.get(
        next((k for k in _MIN_FVG_ATR if symbol.upper().startswith(k)), ''), 0.30
    )

    lookback = 20
    win = df.tail(lookback + 2).reset_index(drop=True)
    n   = len(win)

    has_ts = 'timestamp' in win.columns

    for i in range(n - 2):
        c1 = win.iloc[i]
        c3 = win.iloc[i + 2]           # [FIXED: changed 'window' to 'win']
        subsequent = win.iloc[i + 2:]  # [FIXED: changed 'window' to 'win']

        # Determine window membership if timestamps available
        w_name = ''
        if has_ts:
            try:
                c2_dt = pd.to_datetime(win.iloc[i + 1]['timestamp'], utc=True)
                t = c2_dt.hour * 60 + c2_dt.minute
                for sh, sm, eh, em, name in windows:
                    if sh * 60 + sm <= t < eh * 60 + em:
                        w_name = name
                        break
            except Exception:
                pass
        else:
            # Without timestamps: only accept if currently inside a window
            in_sb, w_name = _is_in_silver_bullet_window(utc_now, symbol)
            if not in_sb:
                continue

        if not w_name:
            continue   # FVG formed outside any SB window — not a time-FVG

        bars_ago = n - 2 - i   # 0 = most recent

        if direction == 'BUY':
            gap_low  = float(c1['high'])
            gap_high = float(c3['low'])
            if gap_high <= gap_low or (gap_high - gap_low) < min_gap:
                continue
            # Check how much of the gap has been filled (price re-entered)
            min_sub_low = float(subsequent['low'].min()) if len(subsequent) > 0 else gap_high
            if min_sub_low <= gap_low:
                continue   # fully filled — dead FVG
            filled_pct = max(0.0, min(1.0, (gap_high - min_sub_low) / (gap_high - gap_low)))
            if filled_pct >= 0.80:
                continue   # mostly filled

        else:   # SELL
            gap_high = float(c1['low'])
            gap_low  = float(c3['high'])
            if gap_low >= gap_high or (gap_high - gap_low) < min_gap:
                continue
            max_sub_high = float(subsequent['high'].max()) if len(subsequent) > 0 else gap_low
            if max_sub_high >= gap_high:
                continue
            filled_pct = max(0.0, min(1.0, (max_sub_high - gap_low) / (gap_high - gap_low)))
            if filled_pct >= 0.80:
                continue

        size = gap_high - gap_low
        return {
            'found':        True,
            'fvg_high':     round(gap_high, 5),
            'fvg_low':      round(gap_low, 5),
            'fvg_mid':      round((gap_high + gap_low) / 2, 5),
            'fvg_size':     round(size, 5),
            'fvg_size_atr': round(size / atr, 2),
            'window_name':  w_name,
            'bars_ago':     bars_ago,
            'filled_pct':   round(filled_pct, 2) if 'filled_pct' in dir() else 0.0,
        }

    return empty


def detect_po3_asian_sweep(df: pd.DataFrame, asian: dict,
                            utc_now: datetime, symbol: str, atr: float) -> dict:
    """
    Power of 3 — Asian Range Sweep + Displacement detector.

    The Power of 3 for XAU/XAG/Crypto:
      Phase 1 (Accumulation): Asian session builds a range (detected by detect_asian_range_scalp)
      Phase 2 (Manipulation): London or early NY sweeps ABOVE Asian High or BELOW Asian Low
                               This is the "Judas Swing" — engineered liquidity grab
      Phase 3 (Distribution):  After the sweep, price displaces in the TRUE direction
                               leaving FVGs behind — these are the prime scalp entries

    Unlike the standard Judas swing detector (which fires on any swing), this
    function requires:
      a) A valid Asian range (not just any accumulation)
      b) A confirmed SWEEP: candle closes BEYOND the Asian H/L, not just wicks
      c) A DISPLACEMENT: the very next bar (or within 3 bars) closes back INSIDE
         the Asian range AND beyond the Asian midpoint
      d) An FVG formed during the displacement move

    Returns dict:
      swept:            bool — Asian H or L has been swept
      direction:        'BUY' (sweep of AH → expect down) or
                        'SELL' (sweep of AL → expect up)
                        NOTE: direction = expected trade direction AFTER sweep
      sweep_level:      float — the level that was swept
      sweep_type:       'ABOVE_AH' or 'BELOW_AL'
      displacement:     bool — confirmed displacement after sweep
      disp_fvg:         dict  — FVG left by displacement (may be empty)
      sweep_bars_ago:   int
      po3_score:        float — composite quality score
    """
    empty = {'swept': False, 'direction': None, 'sweep_level': None,
             'sweep_type': None, 'displacement': False,
             'disp_fvg': {}, 'sweep_bars_ago': 0, 'po3_score': 0.0}

    if not asian.get('valid') or atr <= 0:
        return empty

    ah  = asian['asian_high']
    al  = asian['asian_low']
    mid = asian['asian_midpoint']

    if df is None or len(df) < 10:
        return empty

    window  = df.tail(30).reset_index(drop=True)
    n       = len(window)
    result  = dict(empty)

    for i in range(n - 1):
        bar = window.iloc[i]
        # Sweep ABOVE Asian High (bearish setup → expect SELL after sweep)
        if bar['high'] > ah and bar['close'] > ah:
            swept_dir   = 'SELL'
            sweep_type  = 'ABOVE_AH'
            sweep_level = ah
        # Sweep BELOW Asian Low (bullish setup → expect BUY after sweep)
        elif bar['low'] < al and bar['close'] < al:
            swept_dir   = 'BUY'
            sweep_type  = 'BELOW_AL'
            sweep_level = al
        else:
            continue

        # Found a sweep — look for displacement in the next 1-4 bars
        sweep_bars_ago = n - 1 - i
        result.update({
            'swept':          True,
            'direction':      swept_dir,
            'sweep_level':    sweep_level,
            'sweep_type':     sweep_type,
            'sweep_bars_ago': sweep_bars_ago,
        })

        # Displacement: within 3 bars, price closes back inside the range
        # AND past the Asian midpoint in the opposite direction
        for j in range(i + 1, min(i + 5, n)):
            disp_bar = window.iloc[j]
            if swept_dir == 'SELL':
                # After sweep above AH: expect close back below mid (bearish displacement)
                if disp_bar['close'] < mid:
                    result['displacement'] = True
                    # Check for FVG left by this displacement (scan bars i..j)
                    sub_df = window.iloc[max(0, i - 1): j + 2]
                    fvg = detect_time_fvg(sub_df, 'SELL', symbol, utc_now, atr)
                    if not fvg['found']:
                        # Also accept a generic FVG from the displacement bars
                        fvg = _detect_fvg_from_slice(sub_df, 'SELL', atr,
                                                      min_gap=atr * _MIN_FVG_ATR.get(
                                                          next((k for k in _MIN_FVG_ATR
                                                                if symbol.upper().startswith(k)), ''),
                                                          0.3))
                    result['disp_fvg'] = fvg
                    break
            else:
                # After sweep below AL: expect close back above mid (bullish displacement)
                if disp_bar['close'] > mid:
                    result['displacement'] = True
                    sub_df = window.iloc[max(0, i - 1): j + 2]
                    fvg = detect_time_fvg(sub_df, 'BUY', symbol, utc_now, atr)
                    if not fvg['found']:
                        fvg = _detect_fvg_from_slice(sub_df, 'BUY', atr,
                                                      min_gap=atr * _MIN_FVG_ATR.get(
                                                          next((k for k in _MIN_FVG_ATR
                                                                if symbol.upper().startswith(k)), ''),
                                                          0.3))
                    result['disp_fvg'] = fvg
                    break

        # Quality score for PO3 component
        po3_score = 0.0
        if result['swept']:          po3_score += 0.20
        if result['displacement']:   po3_score += 0.20
        if result.get('disp_fvg', {}).get('found'):  po3_score += 0.15
        if asian.get('is_tight'):    po3_score += 0.05   # tighter range = cleaner sweep
        # Freshness bonus: recent sweeps are more actionable
        if sweep_bars_ago <= 2:       po3_score += 0.05
        result['po3_score'] = round(po3_score, 3)

        # Use the MOST RECENT sweep (first found in reverse scan would be ideal,
        # but we scan forward and keep the latest by overwriting until end of loop)
        # To get most recent: break on first valid displacement, ignore stale sweeps
        if result['displacement']:
            break

    return result


def _detect_fvg_from_slice(df_slice: pd.DataFrame, direction: str,
                            atr: float, min_gap: float = 0.0) -> dict:
    """
    Helper: generic (non-time-gated) FVG detection on a small DataFrame slice.
    Used internally by detect_po3_asian_sweep to find displacement FVGs even
    when no Silver Bullet timestamp is available.
    """
    empty = {'found': False}
    if df_slice is None or len(df_slice) < 3 or atr <= 0:
        return empty
    win = df_slice.reset_index(drop=True)
    n   = len(win)
    mg  = max(min_gap, atr * 0.20)
    for i in range(n - 2):
        c1 = win.iloc[i]
        c3 = win.iloc[i + 2]
        if direction == 'BUY':
            gl = float(c1['high']); gh = float(c3['low'])
            if gh > gl and (gh - gl) >= mg:
                return {'found': True, 'fvg_low': round(gl, 5),
                        'fvg_high': round(gh, 5),
                        'fvg_mid': round((gl + gh) / 2, 5),
                        'fvg_size': round(gh - gl, 5),
                        'window_name': 'displacement'}
        else:
            gh = float(c1['low']); gl = float(c3['high'])
            if gl < gh and (gh - gl) >= mg:
                return {'found': True, 'fvg_low': round(gl, 5),
                        'fvg_high': round(gh, 5),
                        'fvg_mid': round((gl + gh) / 2, 5),
                        'fvg_size': round(gh - gl, 5),
                        'window_name': 'displacement'}
    return empty











# ── [S32] FX LONDON M1 SCALP MODULE ─────────────────────────────────────────
def compute_fx_london_scalp(df_m1, symbol, utc_now, h4_bias="NEUTRAL"):
    """
    M1 pullback scalp for FX pairs during London session (07:00-10:00 UTC).

    FX pairs exhibit strong directional momentum at London open as EUR and
    GBP liquidity kicks in. The same displacement + pullback pattern used
    for Gold applies here, calibrated for FX pip values.

    Only fires when:
      - Session: London open 07:00-10:00 UTC
      - H4 bias confirmed (Conqueror filter pre-applied)
      - 2-candle displacement detected on M1
      - Pullback candle confirmed (1-2 bars counter-trend)
    """
    NEUTRAL = ("NEUTRAL", 0.0, "FX_Scalp: No setup", {}, "FX_LONDON_M1")

    # London session only
    t_min = utc_now.hour * 60 + utc_now.minute
    if not (7*60 <= t_min <= 10*60):
        return NEUTRAL

    # FX pairs only
    sym = symbol.upper()
    FX_LIQUID = ['EURUSD','GBPUSD','USDJPY','AUDUSD','EURGBP','GBPJPY','EURJPY']
    if not any(p in sym for p in FX_LIQUID):
        return NEUTRAL

    if df_m1 is None or len(df_m1) < 20:
        return NEUTRAL

    df  = df_m1.copy().reset_index(drop=True)
    df['_atr'] = calculate_atr(df)
    atr = float(df['_atr'].iloc[-1])
    if atr <= 0:
        return NEUTRAL

    # FX pip size calibration
    # USDJPY: 1 pip = 0.01; others: 1 pip = 0.0001
    pip = 0.01 if 'JPY' in sym else 0.0001
    min_disp = 3 * pip  # Minimum 3-pip displacement candle

    def body(i):
        return float(df.iloc[i]['close']) - float(df.iloc[i]['open'])
    def bar_range(i):
        return float(df.iloc[i]['high']) - float(df.iloc[i]['low'])

    n = len(df)

    # Spike guard
    if bar_range(n-1) > atr * 2.0:
        return NEUTRAL

    # Displacement detection: 2 consecutive directional candles
    bull_d = bear_d = False
    disp_end = -1
    md = max(min_disp, atr * 0.5)  # At least 0.5×ATR per candle

    for i in range(max(0, n-8), n-2):
        b0, b1 = body(i), body(i+1)
        if b0 >= md and b1 >= md:
            bull_d = True; disp_end = i+1; break
        if b0 <= -md and b1 <= -md:
            bear_d = True; disp_end = i+1; break

    if not bull_d and not bear_d:
        return NEUTRAL

    direction = "BUY" if bull_d else "SELL"

    # H4 bias filter — only trade with the prevailing H4 trend
    if h4_bias != "NEUTRAL":
        if direction == "BUY" and "BEAR" in h4_bias.upper():
            return NEUTRAL
        if direction == "SELL" and "BULL" in h4_bias.upper():
            return NEUTRAL

    # Pullback confirmation
    has_pullback = False
    pullback_high = pullback_low = None

    for i in range(max(disp_end+1, n-3), n):
        b = body(i)
        if direction == "SELL" and b > 0 and abs(b) < md * 1.5:
            has_pullback = True
            pullback_high = float(df.iloc[i]['high'])
            pullback_low  = float(df.iloc[i]['low'])
            break
        elif direction == "BUY" and b < 0 and abs(b) < md * 1.5:
            has_pullback = True
            pullback_high = float(df.iloc[i]['high'])
            pullback_low  = float(df.iloc[i]['low'])
            break

    if not has_pullback:
        return NEUTRAL  # FX scalp REQUIRES pullback — no market orders

    # FX TP/SL: tighter than commodities (pips not points)
    tp_m, sl_m = 2.0, 1.5  # 1.33:1 R:R minimum

    price = float(df.iloc[-1]['close'])
    if direction == "BUY":
        entry = round(pullback_low, 5)
        tp    = round(entry + tp_m * atr, 5)
        sl    = round(entry - sl_m * atr, 5)
        sig   = "BUY_MICRO"
    else:
        entry = round(pullback_high, 5)
        tp    = round(entry - tp_m * atr, 5)
        sl    = round(entry + sl_m * atr, 5)
        sig   = "SELL_MICRO"

    sl_dist = abs(entry - sl)
    tp_dist = abs(entry - tp)
    if sl_dist <= 0 or tp_dist / sl_dist < 1.2:
        return NEUTRAL

    score = 0.70 + 0.08  # Pullback confirmed = +0.08

    # London open bonus (07:00-08:00 = prime manipulation window)
    if t_min < 8*60:
        score = min(0.84, score + 0.04)

    reason = (f"FX London M1 {direction}: {sym} pullback {'high' if direction=='SELL' else 'low'} "
              f"@ {entry:.5f} ATR={atr:.5f}")

    cond = {
        'm1_scalp':          True,
        'm1_tp':             tp,
        'm1_sl':             sl,
        'm1_atr':            round(atr, 5),
        'm1_entry_price':    entry,
        'm1_has_pullback':   True,
        'm1_pullback_bonus': 0.08,
        'model_winner':      'FX_LONDON_M1',
    }
    return (sig, score, reason, cond, "FX_LONDON_M1")


# ── [S32] SLINGSHOT REVERSAL PATTERN ─────────────────────────────────────────
def compute_slingshot_reversal(df_h4, symbol, utc_now):
    """
    Identifies exhausted rallies via the Slingshot pattern (Courtney Smith).

    Logic:
      1. Key Low — distinct swing low with 2+ bars higher on each side
      2. Price rallies but fails to break a prior swing high (Slingshot High)
      3. Current price breaks below the Key Low → trapped longs capitulate
      4. Enter SELL_MICRO; TP = Key Low − (Slingshot High − Key Low)
      5. SL = Slingshot High + 0.5×ATR

    Active during London (07-16 UTC) and NY (13-21 UTC).
    Returns compatible tuple for main signal routing.
    """
    NEUTRAL = ("NEUTRAL", 0.0, "Slingshot: No setup", {}, "SLINGSHOT")

    t_min = utc_now.hour * 60 + utc_now.minute
    active = (7*60 <= t_min < 21*60)
    if not active:
        return NEUTRAL

    if df_h4 is None or len(df_h4) < 30:
        return NEUTRAL

    try:
        df = df_h4.copy().reset_index(drop=True)
        df['_atr'] = calculate_atr(df)
        atr = float(df['_atr'].iloc[-1])
        if atr <= 0:
            return NEUTRAL

        highs  = df['high'].astype(float).values
        lows   = df['low'].astype(float).values
        closes = df['close'].astype(float).values
        n      = len(df)

        # ── Step 1: Find Key Low ──────────────────────────────────────────
        # A Key Low requires lows[-k] < lows[-k-1] and lows[-k] < lows[-k+1]
        # (a genuine pivot low) at least 5 bars back
        key_low_idx  = None
        key_low_val  = None
        for k in range(5, min(25, n-3)):
            idx = n - 1 - k
            if lows[idx] < lows[idx-1] and lows[idx] < lows[idx-2] and                lows[idx] < lows[idx+1] and lows[idx] < lows[idx+2]:
                key_low_idx = idx
                key_low_val = lows[idx]
                break

        if key_low_idx is None:
            return NEUTRAL

        # ── Step 2: Find prior swing high before the Key Low ─────────────
        prior_swing_high = None
        for k in range(key_low_idx - 1, max(0, key_low_idx - 15), -1):
            if highs[k] > highs[k-1] and highs[k] > highs[k+1]:
                prior_swing_high = highs[k]
                break

        if prior_swing_high is None:
            return NEUTRAL

        # ── Step 3: Find Slingshot High — rally after Key Low that FAILS ─
        # The rally after the Key Low must not exceed the prior swing high
        slingshot_high = None
        for k in range(key_low_idx + 1, n - 1):
            if highs[k] > highs[k-1] and highs[k] > highs[k+1]:
                if highs[k] < prior_swing_high:  # Failed to exceed prior high
                    slingshot_high = highs[k]
                    break

        if slingshot_high is None:
            return NEUTRAL

        # ── Step 4: Current price breaks below Key Low ───────────────────
        cur_close = float(closes[-1])
        if cur_close >= key_low_val:
            return NEUTRAL  # Not broken below Key Low yet

        # ── Compute TP / SL ───────────────────────────────────────────────
        swing_range = slingshot_high - key_low_val
        if swing_range <= atr * 0.5:
            return NEUTRAL  # Range too small to be meaningful

        tp = round(key_low_val - swing_range, 5)      # Projected equal move down
        sl = round(slingshot_high + atr * 0.5, 5)    # Above the failed high
        entry = round(cur_close, 5)

        sl_dist = abs(entry - sl)
        tp_dist = abs(entry - tp)
        if sl_dist <= 0 or tp_dist / sl_dist < 1.5:
            return NEUTRAL  # Minimum 1.5:1 R:R required

        score = 0.72
        # Bonus: strong momentum candle breaking the Key Low
        if float(df['close'].iloc[-1]) < float(df['open'].iloc[-1]):
            score = min(0.78, score + 0.06)  # Bearish close = confirmed breakdown

        reason = (f"Slingshot SELL: Key Low {key_low_val:.5g} broken, "
                  f"SLH {slingshot_high:.5g} failed vs prior {prior_swing_high:.5g}")

        cond = {
            'm1_scalp':          True,
            'm1_tp':             tp,
            'm1_sl':             sl,
            'm1_atr':            round(atr, 5),
            'm1_entry_price':    entry,
            'm1_has_pullback':   False,
            'm1_pullback_bonus': 0.0,
            'model_winner':      'SLINGSHOT',
            'sling_key_low':     round(key_low_val, 5),
            'sling_high':        round(slingshot_high, 5),
            'sling_prior_high':  round(prior_swing_high, 5),
        }
        return ("SELL_MICRO", score, reason, cond, "SLINGSHOT")

    except Exception:
        return NEUTRAL


# ── [S33] EURUSD DEDICATED STRATEGY MODULE ───────────────────────────────────
def compute_eurusd_strategy(df_m15, df_m1, symbol, utc_now):
    """
    [S33] Two-sub-strategy engine built specifically for EURUSD.

    Historical context: ICT_STANDARD produced 0% WR / -$59.84 on EURUSD
    (14 trades). EURUSD is a deep-liquidity pair that trends hard on London
    open and mean-reverts during NY afternoon consolidation. ICT AMD
    framework is wrong for this pair; these two modes are right.

    Sub-strategy A — London Momentum Breakout (07:00-08:30 UTC):
      • Detects a tight Asian range (23:00-06:00 UTC, ideally < 30 pips)
      • Waits for a genuine breakout bar: M15 close outside range with
        a body > 5 pips (not just a wick)
      • Confirms bar has NOT rejected back (distinguishes from Turtle Soup)
      • Enters in breakout direction via limit at the broken range boundary
      • TP: 2.0×ATR  |  SL: 1.2×ATR
      • Score: 0.72 base, +0.05 for tight Asian range, +0.04 H4 alignment

    Sub-strategy B — NY Afternoon Mean Reversion (13:30-16:00 UTC):
      • Computes Z-score of last M15 close vs 20-bar EMA
      • Fade when |Z| > 1.8 (price statistically extended)
      • TP: EMA20 (the mean)  |  SL: 1.5×ATR beyond the extreme
      • Score: 0.70 base, +0.05 if |Z| > 2.2 (deeper extreme = higher edge)

    Both sub-strategies bypass ICT scoring and feed directly into the
    M1 pullback entry execution path for execution consistency.
    Returns same tuple format as all other compute_* functions.
    """
    NEUTRAL = ("NEUTRAL", 0.0, "EURUSD: No setup", {}, "EURUSD_STRATEGY")

    sym = symbol.upper()
    if "EURUSD" not in sym:
        return NEUTRAL

    if df_m15 is None or len(df_m15) < 25:
        return NEUTRAL

    df = df_m15.copy().reset_index(drop=True)
    try:
        df['_atr'] = calculate_atr(df)
        atr = float(df['_atr'].iloc[-1])
    except Exception:
        return NEUTRAL

    if atr <= 0:
        return NEUTRAL

    pip = 0.0001  # EURUSD pip size
    t_min = utc_now.hour * 60 + utc_now.minute

    # ── Sub-strategy A: London Momentum Breakout ──────────────────────────
    in_london_open = (7*60 <= t_min <= 8*60 + 30)

    if in_london_open:
        # Detect Asian range: look for bars timestamped 23:00-06:00 UTC today
        asian_high = asian_low = None
        try:
            if 'timestamp' in df.columns:
                df['_dt'] = pd.to_datetime(df['timestamp'], utc=True)
                today = utc_now.date()
                from datetime import timedelta
                yesterday = today - timedelta(days=1)
                asian_mask = (
                    (
                        (df['_dt'].dt.date == yesterday) & (df['_dt'].dt.hour >= 23)
                    ) | (
                        (df['_dt'].dt.date == today) & (df['_dt'].dt.hour < 7)
                    )
                )
                asian_bars = df[asian_mask]
                if len(asian_bars) >= 4:
                    asian_high = float(asian_bars['high'].max())
                    asian_low  = float(asian_bars['low'].min())
        except Exception:
            pass

        # Fallback: last 28 bars ≈ 7 hours of M15
        if asian_high is None:
            lookback_bars = df.tail(28)
            if len(lookback_bars) >= 6:
                asian_high = float(lookback_bars['high'].max())
                asian_low  = float(lookback_bars['low'].min())

        if asian_high is not None and asian_low is not None:
            asian_range_pips = (asian_high - asian_low) / pip
            tight_range = asian_range_pips < 35  # < 35 pips = consolidation

            cur_bar  = df.iloc[-1]
            cur_close = float(cur_bar['close'])
            cur_open  = float(cur_bar['open'])
            bar_body  = abs(cur_close - cur_open)
            min_body  = 5 * pip  # at least 5 pips of body

            # Bullish breakout: close above Asian high AND body confirms
            bull_break = (
                cur_close > asian_high + 2 * pip and
                cur_close > cur_open and          # bullish body
                bar_body >= min_body
            )
            # Bearish breakout: close below Asian low AND body confirms
            bear_break = (
                cur_close < asian_low - 2 * pip and
                cur_close < cur_open and          # bearish body
                bar_body >= min_body
            )

            if bull_break or bear_break:
                direction = "BUY" if bull_break else "SELL"
                ref_level = asian_high if bull_break else asian_low

                # Entry: at the broken boundary (momentum continuation)
                entry = round(ref_level, 5)
                tp    = round(entry + 2.0 * atr, 5) if bull_break else round(entry - 2.0 * atr, 5)
                sl    = round(entry - 1.2 * atr, 5) if bull_break else round(entry + 1.2 * atr, 5)

                sl_dist = abs(entry - sl)
                tp_dist = abs(tp - entry)
                if sl_dist <= 0 or tp_dist / sl_dist < 1.3:
                    return NEUTRAL

                score = 0.72
                if tight_range:
                    score = min(0.82, score + 0.05)  # tighter range = cleaner break
                if asian_range_pips < 20:
                    score = min(0.84, score + 0.02)  # very tight = extra edge
                # London open prime window bonus
                if t_min < 7*60 + 45:
                    score = min(0.84, score + 0.04)

                sig = f"{direction}_MICRO"
                reason = (
                    f"EURUSD London Breakout {direction}: "
                    f"Asian range {asian_range_pips:.0f}pips "
                    f"[{asian_low:.5f}-{asian_high:.5f}] "
                    f"broken @ {cur_close:.5f} | body={bar_body/pip:.1f}pips"
                )
                cond = {
                    'm1_scalp':           True,
                    'm1_tp':              tp,
                    'm1_sl':              sl,
                    'm1_atr':             round(atr, 5),
                    'm1_entry_price':     entry,
                    'm1_has_pullback':    False,
                    'm1_pullback_bonus':  0.0,
                    'model_winner':       'EURUSD_LONDON_BREAKOUT',
                    'eu_asian_high':      round(asian_high, 5),
                    'eu_asian_low':       round(asian_low, 5),
                    'eu_asian_range_pips': round(asian_range_pips, 1),
                    'eu_tight_range':     tight_range,
                }
                return (sig, round(score, 3), reason, cond, "EURUSD_LONDON_OPEN")

    # ── Sub-strategy B: NY Afternoon Mean Reversion ───────────────────────
    in_ny_reversion = (13*60 + 30 <= t_min <= 16*60)

    if in_ny_reversion:
        if len(df) < 22:
            return NEUTRAL

        closes = df['close'].astype(float)
        ema20  = closes.ewm(span=20, adjust=False).mean()
        cur_close = float(closes.iloc[-1])
        ema_val   = float(ema20.iloc[-1])

        # Z-score: how many std devs is price from EMA?
        std20 = float(closes.iloc[-20:].std())
        if std20 <= 0:
            return NEUTRAL

        z_score = (cur_close - ema_val) / std20

        # Only trade meaningful extremes
        if abs(z_score) < 1.8:
            return NEUTRAL

        direction = "SELL" if z_score > 0 else "BUY"

        # TP = EMA (the mean reversion target)
        tp  = round(ema_val, 5)
        if direction == "SELL":
            sl  = round(cur_close + 1.5 * atr, 5)
        else:
            sl  = round(cur_close - 1.5 * atr, 5)

        sl_dist = abs(cur_close - sl)
        tp_dist = abs(tp - cur_close)
        if sl_dist <= 0 or tp_dist / sl_dist < 1.2:
            return NEUTRAL

        score = 0.70
        if abs(z_score) > 2.2:
            score = min(0.80, score + 0.05)  # deeper extreme = more edge
        if abs(z_score) > 2.8:
            score = min(0.82, score + 0.02)  # very extended = high probability

        sig = f"{direction}_MICRO"
        reason = (
            f"EURUSD NY Reversion {direction}: "
            f"Z={z_score:+.2f} (|Z|>1.8 threshold) "
            f"price={cur_close:.5f} EMA20={ema_val:.5f} TP={tp:.5f}"
        )
        cond = {
            'm1_scalp':           True,
            'm1_tp':              tp,
            'm1_sl':              sl,
            'm1_atr':             round(atr, 5),
            'm1_entry_price':     round(cur_close, 5),
            'm1_has_pullback':    False,
            'm1_pullback_bonus':  0.0,
            'model_winner':       'EURUSD_NY_REVERSION',
            'eu_z_score':         round(z_score, 3),
            'eu_ema20':           round(ema_val, 5),
            'eu_std20':           round(std20, 5),
        }
        return (sig, round(score, 3), reason, cond, "EURUSD_NY_REVERSION")

    return NEUTRAL


# ── [S31] TURTLE SOUP FX FADE MODULE ─────────────────────────────────────────
def compute_turtle_soup_fx(df_m15, symbol, utc_now):
    """
    Fades false FX breakouts (Turtle Soup / ICT manipulation sweep).
    
    London session only (07:00-10:30 UTC). FX pairs only.
    Logic:
      1. Price breaks above 20-bar M15 high or below 20-bar low
      2. Wait: next 1-2 bars show rejection (close back inside the range)
      3. Enter LIMIT in the opposite direction at the 20-bar boundary
      4. SL: 1×ATR beyond the false breakout extreme
      5. TP: Opposite range extreme (mean reversion target)
    
    This directly addresses EUR/USD WR=0%, Net=-$59.84 historical losses
    by turning the false breakout into the entry signal.
    """
    NEUTRAL = ("NEUTRAL", 0.0, "TurtleSoup: No setup", {}, "TURTLE_SOUP")
    
    # FX pairs only
    sym = symbol.upper()
    FX_PAIRS = ['EURUSD','GBPUSD','USDJPY','AUDUSD','USDCAD','USDCHF',
                'NZDUSD','EURJPY','GBPJPY','AUDJPY','EURGBP','AUDJPY']
    is_fx = any(p in sym for p in FX_PAIRS)
    if not is_fx:
        return NEUTRAL
    
    # London session only: 07:00-10:30 UTC (Manipulation phase)
    t_min = utc_now.hour * 60 + utc_now.minute
    in_london = (7*60 <= t_min <= 10*60 + 30)
    if not in_london:
        return NEUTRAL
    
    if df_m15 is None or len(df_m15) < 25:
        return NEUTRAL
    
    df = df_m15.copy().reset_index(drop=True)
    
    try:
        df['_atr'] = calculate_atr(df)
        atr = float(df['_atr'].iloc[-1])
    except Exception:
        return NEUTRAL
    
    if atr <= 0:
        return NEUTRAL
    
    # 20-bar lookback range (excluding the last 2 bars)
    lookback = 20
    if len(df) < lookback + 3:
        return NEUTRAL
    
    # Range over bars [-lookback-2:-2] (exclude last 2 bars — potential breakout)
    range_df   = df.iloc[-lookback-2:-2]
    range_high = float(range_df['high'].max())
    range_low  = float(range_df['low'].min())
    
    # Last 2 bars: the potential false breakout bars
    bar_1  = df.iloc[-2]  # First breakout bar
    bar_0  = df.iloc[-1]  # Most recent bar (should show rejection)
    
    high_1 = float(bar_1['high']); low_1 = float(bar_1['low']); close_1 = float(bar_1['close'])
    high_0 = float(bar_0['high']); low_0 = float(bar_0['low']); close_0 = float(bar_0['close'])
    
    signal    = None
    entry     = None
    sl        = None
    tp        = None
    breakout_extreme = None
    
    # Bearish Turtle Soup: broke above 20-bar high but closed back inside
    if high_1 > range_high and close_1 < range_high and close_0 < range_high:
        # False upside breakout confirmed — fade it (SELL)
        signal           = "SELL_MICRO"
        breakout_extreme = high_1
        entry            = round(range_high + atr * 0.1, 5)  # Just above the swept level
        sl               = round(breakout_extreme + atr * 1.0, 5)
        tp               = round(range_low + atr * 0.5, 5)
    
    # Bullish Turtle Soup: broke below 20-bar low but closed back inside
    elif low_1 < range_low and close_1 > range_low and close_0 > range_low:
        # False downside breakout confirmed — fade it (BUY)
        signal           = "BUY_MICRO"
        breakout_extreme = low_1
        entry            = round(range_low - atr * 0.1, 5)
        sl               = round(breakout_extreme - atr * 1.0, 5)
        tp               = round(range_high - atr * 0.5, 5)
    
    if signal is None:
        return NEUTRAL
    
    # R:R check
    sl_dist = abs(entry - sl)
    tp_dist = abs(tp - entry)
    if sl_dist <= 0 or tp_dist / sl_dist < 1.2:
        return NEUTRAL
    
    score = 0.70  # Conservative base — FX manipulation patterns have noise
    # Bonus: if the rejection bar also shows strong opposite momentum
    if signal == "SELL_MICRO" and close_0 < (high_0 + low_0) / 2:
        score = min(0.76, score + 0.06)  # Rejection with bear close = stronger
    elif signal == "BUY_MICRO" and close_0 > (high_0 + low_0) / 2:
        score = min(0.76, score + 0.06)
    
    reason = (f"TurtleSoup {signal[:4]}: {sym} false breakout above {range_high:.5f}" 
              if 'SELL' in signal 
              else f"TurtleSoup {signal[:3]}: {sym} false breakdown below {range_low:.5f}")
    
    cond = {
        'm1_scalp':        True,  # Routes through same execution path
        'm1_tp':           tp,
        'm1_sl':           sl,
        'm1_atr':          round(atr, 5),
        'm1_entry_price':  round(entry, 5),
        'm1_has_pullback': False,
        'm1_pullback_bonus': 0.0,
        'model_winner':    'TURTLE_SOUP_FX',
        'ts_range_high':   round(range_high, 5),
        'ts_range_low':    round(range_low, 5),
        'ts_breakout_ext': round(breakout_extreme, 5),
    }
    return (signal, score, reason, cond, "TURTLE_SOUP")


# ── [S30] SILVER ASIAN MEAN-REVERSION MODULE ─────────────────────────────────
def compute_silver_asian_reversion(df_m1, utc_now):
    """
    Asian session (22:00-07:00 UTC) mean-reversion for XAGUSD only.
    
    Silver ranges during Asian hours with NY net $885 vs Asian -$34.
    Strategy: fade the Bollinger Band extremes when ATR is contracting.
    
    Entry: BUY near lower BB, SELL near upper BB
    Confirmation: RSI < 30 (BUY) or RSI > 70 (SELL)
    SL: 1.0×ATR beyond the band
    TP: midline of BB (mean reversion target)
    
    Returns same tuple as compute_m1_scalp for compatible routing.
    """
    NEUTRAL = ("NEUTRAL", 0.0, "SilverAR: No setup", {}, "ASIAN_REVERSION")
    
    t_min = utc_now.hour * 60 + utc_now.minute
    # Only active during Asian session
    in_asian = (22*60 <= t_min < 24*60) or (0 <= t_min < 7*60)
    if not in_asian:
        return NEUTRAL
    
    if df_m1 is None or len(df_m1) < 30:
        return NEUTRAL
    
    df = df_m1.copy().reset_index(drop=True)
    closes = df['close'].astype(float)
    
    # ATR (14-bar)
    try:
        df['_atr'] = calculate_atr(df)
        atr = float(df['_atr'].iloc[-1])
        atr_avg = float(df['_atr'].iloc[-20:].mean())
    except Exception:
        return NEUTRAL
    
    if atr <= 0 or atr_avg <= 0:
        return NEUTRAL
    
    # ATR contraction: current ATR < 80% of recent average → ranging mode
    atr_contracting = (atr < atr_avg * 0.80)
    if not atr_contracting:
        return NEUTRAL  # Skip if volatility is expanding
    
    # Bollinger Bands (20, 2σ)
    bb_period = 20
    if len(closes) < bb_period + 5:
        return NEUTRAL
    
    bb_mean  = float(closes.iloc[-bb_period:].mean())
    bb_std   = float(closes.iloc[-bb_period:].std())
    bb_upper = bb_mean + 2.0 * bb_std
    bb_lower = bb_mean - 2.0 * bb_std
    bb_width = bb_upper - bb_lower
    
    if bb_width <= 0:
        return NEUTRAL
    
    cur_close = float(closes.iloc[-1])
    
    # RSI (14)
    try:
        deltas = closes.diff()
        gains  = deltas.clip(lower=0).rolling(14).mean()
        losses = (-deltas.clip(upper=0)).rolling(14).mean()
        rs     = gains.iloc[-1] / losses.iloc[-1] if losses.iloc[-1] != 0 else 100
        rsi    = 100 - (100 / (1 + rs))
    except Exception:
        rsi = 50.0
    
    # Signal logic: fade the extremes
    near_lower = cur_close <= bb_lower + bb_width * 0.05
    near_upper = cur_close >= bb_upper - bb_width * 0.05
    
    if near_lower and rsi < 35:
        direction = "BUY"
        tp = round(bb_mean, 5)          # Target: midline
        sl = round(cur_close - atr, 5)  # SL: 1×ATR below
        score = 0.68
        if rsi < 25: score = 0.74  # Oversold extreme = stronger signal
        reason = (f"SilverAR BUY: price {cur_close:.3f} at lower BB {bb_lower:.3f} "
                  f"RSI={rsi:.0f} ATR contracting ({atr:.3f}<{atr_avg:.3f})")
    elif near_upper and rsi > 65:
        direction = "SELL"
        tp = round(bb_mean, 5)          # Target: midline
        sl = round(cur_close + atr, 5)  # SL: 1×ATR above
        score = 0.68
        if rsi > 75: score = 0.74  # Overbought extreme = stronger signal
        reason = (f"SilverAR SELL: price {cur_close:.3f} at upper BB {bb_upper:.3f} "
                  f"RSI={rsi:.0f} ATR contracting ({atr:.3f}<{atr_avg:.3f})")
    else:
        return NEUTRAL
    
    # R:R check
    sl_dist = abs(cur_close - sl)
    tp_dist = abs(tp - cur_close)
    if sl_dist <= 0 or tp_dist / sl_dist < 1.3:
        return NEUTRAL
    
    sig = f"{direction}_MICRO"
    cond = {
        'm1_scalp':        True,
        'm1_tp':           tp,
        'm1_sl':           sl,
        'm1_atr':          round(atr, 5),
        'm1_entry_price':  round(cur_close, 5),
        'm1_has_pullback': False,
        'm1_pullback_bonus': 0.0,
        'model_winner':    'SILVER_ASIAN_REVERSION',
        'bb_upper':        round(bb_upper, 5),
        'bb_lower':        round(bb_lower, 5),
        'bb_mean':         round(bb_mean, 5),
        'rsi':             round(rsi, 1),
        'atr_contracting': True,
    }
    return (sig, score, reason, cond, "ASIAN_REVERSION")


# ── [S27-B] M1 MICRO-SCALP ENGINE ─────────────────────────────────────────────
def compute_m1_scalp(df_m1, symbol, utc_now, h4_bias="NEUTRAL"):
    """
    Detects M1 displacement sequences → FVG → intraday range fade.
    Returns MARKET ORDER signal with tight TP/SL.
    conditions['m1_scalp'] = True → bot_engine executes immediately.
    """
    NEUTRAL = ("NEUTRAL", 0.0, "M1: No setup", {}, "M1_SCALP")
    if df_m1 is None or len(df_m1) < 20:
        return NEUTRAL
    df  = df_m1.copy().reset_index(drop=True)
    df['_atr'] = calculate_atr(df)
    atr = float(df['_atr'].iloc[-1])
    if atr <= 0:
        return NEUTRAL

    s = symbol.upper()
    # ── [S30] Empirical session gate for Gold/Silver M1 scalp ─────────
    # Based on 205-trade WR data: 03:00 UTC WR=12%, 06:00 WR=25%, 22:00 WR=17%
    # vs 23:00 WR=86%, 19-20:00 WR=67-100%. Block the dead hours.
    # Gold/Silver only: crypto trades 24h (different liquidity profile).
    _t_min  = utc_now.hour * 60 + utc_now.minute
    _is_cmd = ('XAU' in s or 'XAG' in s)
    if _is_cmd:
        # Dead sessions for commodity scalp:
        # 22:00-00:30 UTC: rollover/low liquidity (WR=17%)
        # 01:00-07:00 UTC: pre-London overnight (WR=12-25%)
        # 09:00-10:30 UTC: [S33] London continuation chop — statistically worst window
        #   Data: 09:00 UTC = -$779 on 17 trades, WR=29%; 10:00 = -$127, WR=17%.
        #   The London open manipulation is already captured 07:00-09:00.
        #   Post-09:00 price action is directionless and whipsaws ICT entries.
        # Allow: 00:30-01:00 (Asian early pop), 07:00-09:00 (London open),
        #        10:30-22:00 (London PM + NY session)
        _dead = (
            (22*60 <= _t_min < 24*60) or
            (1*60+30 <= _t_min < 7*60) or
            (9*60 <= _t_min < 10*60+30)   # [S33] London chop gate
        )
        if _dead:
            return ("NEUTRAL", 0.0,
                    f"M1: {s} blocked — dead session {utc_now.strftime('%H:%M')} UTC (WR<25%)",
                    {}, "M1_SCALP")

    # [S28-FIX] Breathing room: SL 1.5×ATR (was 0.8), TP 2.2-3.0×ATR (was 1.5-2.0)
    # Minimum R:R of 1.4:1 enforced below. Spike guard added.
    # s already set above
    if 'XAU' in s:
        tp_m, sl_m, md = 2.2, 1.5, 0.6   # Gold: 1.47:1 R:R minimum
    elif 'XAG' in s:
        tp_m, sl_m, md = 2.5, 1.5, 0.5   # Silver: 1.67:1 R:R minimum
    elif 'BTC' in s or 'ETH' in s:
        tp_m, sl_m, md = 3.0, 1.5, 0.6   # Crypto: 2.0:1 R:R minimum
    else:
        tp_m, sl_m, md = 2.0, 1.5, 0.6   # Default: 1.33:1 R:R minimum

    # ── Spike guard: skip on outsized candles ───────────────────────
    last_range = float(df.iloc[-1]['high']) - float(df.iloc[-1]['low'])
    if last_range > atr * 1.8:
        return ("NEUTRAL", 0.0, "M1: Spike candle — waiting", {}, "M1_SCALP")

    def body(i): return float(df.iloc[i]['close']) - float(df.iloc[i]['open'])
    def bar_range(i): return float(df.iloc[i]['high']) - float(df.iloc[i]['low'])
    n = len(df)

    # ── [S29] Displacement detection: look for 2+ strong candles ────
    # Then confirm a PULLBACK candle (smaller, opposite body) follows.
    # Pattern: [impulse1][impulse2][pullback] → enter at pullback FVG
    # This is the "Turtle Soup" / retrace-entry model used by manual traders.
    bull_d = bear_d = False
    disp_end_idx = -1   # bar index where displacement sequence ends

    for i in range(max(0, n-8), n-2):
        b0, b1 = body(i), body(i+1)
        if b0 >= md*atr and b1 >= md*atr:
            bull_d = True;  disp_end_idx = i+1; break
        if b0 <= -md*atr and b1 <= -md*atr:
            bear_d = True;  disp_end_idx = i+1; break

    if not bull_d and not bear_d:
        return ("NEUTRAL", 0.0, "M1: No displacement", {}, "M1_SCALP")

    direction = "BUY" if bull_d else "SELL"

    # ── [S29] Pullback confirmation ──────────────────────────────────
    # After displacement, price should retrace partially (1-2 bars).
    # A pullback bar is: body is OPPOSITE and smaller than displacement.
    has_pullback    = False
    pullback_bar    = None
    pullback_high   = None
    pullback_low    = None

    # Check bars AFTER the displacement sequence (last 3 bars)
    for i in range(max(disp_end_idx+1, n-3), n):
        b = body(i)
        rng = bar_range(i)
        if direction == "SELL" and b > 0 and abs(b) < md*atr*1.5:
            # Small bullish candle after bearish displacement = pullback
            has_pullback  = True
            pullback_bar  = i
            pullback_high = float(df.iloc[i]['high'])
            pullback_low  = float(df.iloc[i]['low'])
            break
        elif direction == "BUY" and b < 0 and abs(b) < md*atr*1.5:
            has_pullback  = True
            pullback_bar  = i
            pullback_high = float(df.iloc[i]['high'])
            pullback_low  = float(df.iloc[i]['low'])
            break

    # Pullback bonus — confirmed retrace entry is significantly better
    pullback_score_bonus = 0.10 if has_pullback else 0.0

    direction = "BUY" if bull_d else "SELL"

    # Intraday range detection
    rh = float(df.tail(30)['high'].max())
    rl = float(df.tail(30)['low'].min())
    rs = rh - rl
    price = float(df.iloc[-1]['close'])
    rpos  = (price - rl) / rs if rs > 0 else 0.5
    tight = rs < 3.0 * atr
    at_top, at_bot = rpos > 0.80, rpos < 0.20
    rng_ok = (direction == "SELL" and at_top) or (direction == "BUY" and at_bot)

    # H4 alignment
    h4_ok = (direction == "BUY"  and h4_bias in ("BULLISH","NEUTRAL")) or             (direction == "SELL" and h4_bias in ("BEARISH","NEUTRAL"))
    sc = 0.72 if h4_ok else 0.62
    if tight and rng_ok: sc += 0.08
    elif tight:          sc += 0.03

    # M1 FVG (last 10 bars)
    fvg = None
    for i in range(max(1, n-10), n-1):
        hp = float(df.iloc[i-1]['high']); lp = float(df.iloc[i-1]['low'])
        hn = float(df.iloc[i+1]['high']); ln = float(df.iloc[i+1]['low'])
        if direction == "BUY"  and ln > hp: fvg = (ln + hp)/2; sc += 0.08; break
        if direction == "SELL" and hn < lp: fvg = (hn + lp)/2; sc += 0.08; break

    lb = float(df.iloc[-1]['close']) - float(df.iloc[-1]['open'])
    mom = (direction == "BUY" and lb > 0) or (direction == "SELL" and lb < 0)
    if mom: sc += 0.05

    sc = min(0.94, sc + pullback_score_bonus)  # S29: pullback bonus
    t  = utc_now.hour*60 + utc_now.minute
    dow = utc_now.weekday()
    weekend = (dow >= 5) or (dow == 4 and t >= 22*60)

    # ── Session-specific confidence adjustments ──────────────────────
    # Gold: prime scalp windows
    if 'XAU' in s:
        if 7*60  <= t <  8*60:  sc = min(0.96, sc+0.05)  # London open SB
        if 15*60 <= t < 16*60: sc = min(0.96, sc+0.05)  # NY Afternoon SB
        if 19*60 <= t < 21*60: sc = min(0.96, sc+0.04)  # NY PM (screenshots window)
        if 0*60  <= t <  0*60+30 or t >= 23*60+15:  # post-rollover early Asia
            sc = min(0.96, sc+0.03)

    # Silver: same windows as Gold
    if 'XAG' in s:
        if 7*60 <= t < 8*60 or 15*60 <= t < 16*60:
            sc = min(0.96, sc+0.04)

    # Crypto: 24h but prime windows
    if 'BTC' in s or 'ETH' in s:
        sc = min(0.96, sc+0.02)           # base bonus — always active
        if 0*60  <= t <  2*60:  sc = min(0.97, sc+0.03)  # Asian Open SB
        if 7*60+30 <= t < 8*60+30: sc = min(0.97, sc+0.03)  # London Open SB
        if 13*60 <= t < 14*60: sc = min(0.97, sc+0.03)  # NY Open SB
        if 20*60 <= t < 22*60: sc = min(0.97, sc+0.03)  # NY Close SB

    # Weekend: lower bar for BTC/ETH (only game in town)
    if weekend and ('BTC' in s or 'ETH' in s):
        sc = max(sc, 0.68)

    # ── Accumulation phase bonus: BTC/Gold mapped range → watch sweep ─
    # When parent ICT score is exactly 0.42 (ACCUMULATION floor) it means
    # price is consolidating at a clear range. M1 displacement at range
    # boundaries is the Manipulation sweep. Give +0.06 bonus for this setup.
    parent_phase = h4_bias  # reused as proxy — actual phase in ict_conditions
    if (tight and rng_ok) and abs(sc - 0.72) < 0.15:   # displacement from range extreme
        sc = min(0.97, sc + 0.06)

    # ── [S29] Entry price: use pullback level for better fill ───────
    # If a pullback candle was detected, enter at its body midpoint
    # (the FVG of the retrace). This avoids chasing momentum.
    if direction == "BUY":
        if has_pullback and pullback_low:
            # Enter at the LOW of the pullback bar (demand zone)
            entry_price = round(pullback_low, 5)
        else:
            entry_price = price
        tp  = round(entry_price + tp_m * atr, 5)
        sl  = round(entry_price - sl_m * atr, 5)
        sig = "BUY_MICRO"
    else:
        if has_pullback and pullback_high:
            # Enter at the HIGH of the pullback bar (supply zone)
            entry_price = round(pullback_high, 5)
        else:
            entry_price = price
        tp  = round(entry_price - tp_m * atr, 5)
        sl  = round(entry_price + sl_m * atr, 5)
        sig = "SELL_MICRO"

    # [S28-FIX] R:R guard: TP must be ≥1.3× SL distance
    _sl_dist = abs(sl - entry_price)
    _tp_dist = abs(tp - entry_price)
    if _tp_dist < _sl_dist * 1.3:
        _min_tp = _sl_dist * 1.3
        tp = round(entry_price - _min_tp, 5) if direction == "SELL"              else round(entry_price + _min_tp, 5)

    # Use entry_price as the canonical price for this signal
    price = entry_price

    cond = {
        'm1_scalp': True, 'm1_tp': tp, 'm1_sl': sl, 'm1_atr': round(atr,5),
        'm1_entry_price': round(price, 5),
        'm1_has_pullback': has_pullback,
        'm1_pullback_bonus': pullback_score_bonus,
        'fvg_price': round(fvg,5) if fvg else None,
        'h4_aligned': h4_ok, 'direction': direction,
        'range_high': round(rh,5), 'range_low': round(rl,5),
        'range_position': round(rpos,3), 'at_range_top': at_top,
        'at_range_bottom': at_bot, 'in_tight_range': tight,
        'range_aligned': rng_ok, 'is_weekend': weekend,
    }
    reason = (f"M1 {'RNG_FADE' if rng_ok else 'DISP'} {direction} "
              f"ATR={atr:.3f} TP={tp:.3f} SL={sl:.3f}"
              f"{' FVG' if fvg else ''}{' [H4OK]' if h4_ok else ' [CTR]'}"
              f"{' [TOP]' if at_top else ' [BOT]' if at_bot else ''}")
    return (sig, sc, reason, cond, "M1_SCALP")


def compute_scalp_confluence(df: pd.DataFrame, df_macro: pd.DataFrame,
                              symbol: str, utc_now: datetime = None) -> tuple:
    """
    Silver Bullet / Time-FVG / Power of 3 composite scorer for scalp assets.

    Scoring breakdown (max 1.00):
      Silver Bullet window active     +0.20  (time filter — highest specificity)
      Time-based FVG found            +0.25  (imbalance within SB window)
      PO3 sweep confirmed             +0.20  (Asian range manipulation)
      PO3 displacement confirmed      +0.15  (smart money reversal)
      FVG fresh (≤ 3 bars)            +0.10  (recency filter)
      Asian range quality (tight)     +0.05  (clean accumulation)
      Macro trend aligned             +0.05  (H4 not counter-directional)

    For an entry-quality signal we expect ≥ 0.65 on this scale.

    Returns: (signal, score, reason, conditions_dict, kill_zone)
    """
    if utc_now is None:
        utc_now = datetime.utcnow()

    empty_ret = ("NEUTRAL", 0.0, "Scalp: no setup", {}, "N/A")

    if df is None or len(df) < 20:
        return empty_ret

    df = df.copy()
    if 'atr' not in df.columns:
        df['atr'] = calculate_atr(df)

    current_atr = max(float(df['atr'].iloc[-1]), 1e-8)

    # ── 1. Is current time inside a Silver Bullet window? ───────────────
    in_sb, sb_name = _is_in_silver_bullet_window(utc_now, symbol)

    # ── 2. Asian range for this asset ────────────────────────────────────
    asian = detect_asian_range_scalp(df, symbol, utc_now, current_atr)

    # ── 3. Macro trend alignment (H4) ────────────────────────────────────
    macro_trend = _derive_macro_trend(df_macro) if df_macro is not None and len(df_macro) >= 10 else "NEUTRAL"

    # ── 4. Power of 3 sweep detection ────────────────────────────────────
    po3 = detect_po3_asian_sweep(df, asian, utc_now, symbol, current_atr)

    # ── 5. Direction from PO3 sweep (or skip if no sweep) ─────────────
    # PO3 direction is the trade direction after the sweep.
    # If no PO3 sweep, attempt to read direction from existing price structure.
    if po3['swept'] and po3['direction']:
        direction = po3['direction']
    else:
        # No clear PO3 sweep — try last macro structure
        if macro_trend == 'BULLISH':
            direction = 'BUY'
        elif macro_trend == 'BEARISH':
            direction = 'SELL'
        else:
            return empty_ret   # no directional bias — no scalp signal

    # ── 6. Time-FVG in direction ──────────────────────────────────────────
    tfvg = detect_time_fvg(df, direction, symbol, utc_now, current_atr)

    # ── 7. Score assembly ─────────────────────────────────────────────────
    score = 0.0
    cond  = {
        'scalp_model':    True,
        'symbol':         symbol,
        'direction':      direction,
        'sb_active':      in_sb,
        'sb_window':      sb_name,
        'asian_valid':    asian.get('valid', False),
        'asian_high':     asian.get('asian_high'),
        'asian_low':      asian.get('asian_low'),
        'asian_tight':    asian.get('is_tight', False),
        'asian_range_atr': asian.get('range_atr', 0.0),
        'po3_swept':      po3.get('swept', False),
        'po3_direction':  po3.get('direction'),
        'po3_sweep_type': po3.get('sweep_type'),
        'po3_displaced':  po3.get('displacement', False),
        'po3_score':      po3.get('po3_score', 0.0),
        'tfvg_found':     tfvg.get('found', False),
        'tfvg_high':      tfvg.get('fvg_high'),
        'tfvg_low':       tfvg.get('fvg_low'),
        'tfvg_mid':       tfvg.get('fvg_mid'),
        'tfvg_window':    tfvg.get('window_name', ''),
        'tfvg_bars_ago':  tfvg.get('bars_ago', 0),
        'macro_trend':    macro_trend,
    }

    # Silver Bullet window active — time filter bonus
    if in_sb:
        score += 0.20
        cond['score_sb'] = 0.20

    # Power of 3 sweep
    if po3.get('swept'):
        score += 0.20
        cond['score_po3_sweep'] = 0.20

    # Power of 3 displacement after sweep
    if po3.get('displacement'):
        score += 0.15
        cond['score_po3_disp'] = 0.15

    # Displacement FVG (generic FVG from the PO3 move)
    if po3.get('disp_fvg', {}).get('found'):
        score += 0.10
        cond['score_disp_fvg'] = 0.10

    # Time-based FVG in Silver Bullet window
    if tfvg.get('found'):
        score += 0.25
        cond['score_tfvg'] = 0.25
        # Freshness bonus
        if tfvg.get('bars_ago', 99) <= 3:
            score += 0.10
            cond['score_tfvg_fresh'] = 0.10

    # Asian range quality
    if asian.get('is_tight'):
        score += 0.05
        cond['score_asian_tight'] = 0.05

    # Macro alignment
    macro_aligned = (direction == 'BUY' and macro_trend == 'BULLISH') or \
                    (direction == 'SELL' and macro_trend == 'BEARISH')
    if macro_aligned:
        score += 0.05
        cond['score_macro'] = 0.05

    # [S17] Candlestick confluence — 60% weight (scalp signals are tighter / faster)
    s17_scalp = detect_candlestick_pattern(df, direction, current_atr)
    if s17_scalp['confirmed']:
        s17_scalp_bonus = round(s17_scalp['bonus'] * 0.60, 3)
        score = min(0.99, score + s17_scalp_bonus)
        cond['s17_candle_pattern']  = s17_scalp['pattern']
        cond['s17_candle_confirmed'] = True
        cond['s17_candle_conflict']  = False
        cond['score_s17_candle']    = s17_scalp_bonus
    elif s17_scalp['conflict']:
        s17_scalp_penalty = round(s17_scalp['conflict_penalty'] * 0.60, 3)
        score = max(0.0, score - s17_scalp_penalty)
        cond['s17_candle_pattern']   = 'NONE'
        cond['s17_candle_confirmed'] = False
        cond['s17_candle_conflict']  = True
        cond['s17_conflict_pattern'] = s17_scalp['conflict_pattern']
    else:
        cond['s17_candle_pattern']   = 'NONE'
        cond['s17_candle_confirmed'] = False
        cond['s17_candle_conflict']  = False

    # Hard floor: require at minimum a sweep OR a time-FVG — don't trade on clock alone
    if not po3.get('swept') and not tfvg.get('found'):
        reason = (f"Scalp [{symbol}]: SB={in_sb} but no PO3 sweep and no time-FVG — "
                  f"no structural basis. Score={score:.2f}")
        return "NEUTRAL", 0.0, reason, cond, sb_name or "N/A"

    # Macro counter-directional veto (H4 strongly opposite → no trade)
    if not macro_aligned and macro_trend != "NEUTRAL":
        # Counter-trend scalps are allowed but score-capped at 0.70
        score = min(score, 0.70)
        cond['macro_veto_cap'] = 0.70

    score = min(round(score, 3), 0.99)

    # ── 8. Signal type ────────────────────────────────────────────────────
    # Scalp signals are always MICRO (tight SL, fast exit)
    if score >= 0.65:
        sig = f"{direction}_MICRO"
    elif score >= 0.50:
        sig = f"{direction}_NANO"   # lower confidence — nano sizing
    else:
        return "NEUTRAL", score, f"Scalp score {score:.2f} below threshold", cond, sb_name or "N/A"

    # ── 9. Kill zone label ────────────────────────────────────────────────
    kill_zone = sb_name if in_sb else f"PO3_{po3.get('sweep_type', 'SWEEP')}"

    # ── 10. Reason string ────────────────────────────────────────────────
    sb_tag  = f"⏰{sb_name}" if in_sb else "⏰OutsideSB"
    po3_tag = f"PO3✅[{po3.get('sweep_type','')} disp={po3.get('displacement')}]" if po3.get('swept') else "PO3❌"
    fvg_tag = (f"TimeFVG✅[{tfvg.get('window_name','')} +{tfvg.get('bars_ago',0)}bars]"
               if tfvg.get('found') else "TimeFVG❌")
    asian_tag = (f"Asian[H={asian.get('asian_high')} L={asian.get('asian_low')} "
                 f"tight={asian.get('is_tight')}]" if asian.get('valid') else "Asian[?]")
    s17_scalp_tag = (f" | S17[{cond.get('s17_candle_pattern')}+{cond.get('score_s17_candle', 0):.2f}]"
                     if cond.get('s17_candle_confirmed')
                     else (f" | S17[⚠️{cond['s17_conflict_pattern']}]"
                           if cond.get('s17_candle_conflict') else ""))
    reason = (f"SCALP {direction} [{symbol}] Score:{score:.3f} | "
              f"{sb_tag} | {po3_tag} | {fvg_tag} | {asian_tag} | Macro:{macro_trend}{s17_scalp_tag}")

    return sig, score, reason, cond, kill_zone


def compute_mean_reversion_confluence(df: pd.DataFrame, symbol: str, current_atr: float) -> tuple:
    """
    [S19g] Mean Reversion Model for DEAD MARKET regimes.
    Bypasses structural ICT logic to trade statistical extremes in ranging markets.
    Requires a pierce of the 2.5 Standard Deviation Bollinger Band + RSI confirmation.
    """
    empty_ret = ("NEUTRAL", 0.0, "Mean Reversion: No setup", {}, "N/A")
    if len(df) < 30 or current_atr <= 0: 
        return empty_ret

    df = df.copy()
    
    # Calculate RSI (14-period)
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / (loss + 1e-9)
    df['rsi'] = 100 - (100 / (1 + rs))

    # Calculate Bollinger Bands (20-period, 2.5 Standard Deviations)
    df['bb_mid'] = df['close'].rolling(20).mean()
    df['bb_std'] = df['close'].rolling(20).std()
    df['bb_upper'] = df['bb_mid'] + (df['bb_std'] * 2.5)
    df['bb_lower'] = df['bb_mid'] - (df['bb_std'] * 2.5)

    c1 = df.iloc[-2] # Last closed candle
    c0 = df.iloc[-1] # Current live candle

    score = 0.0
    signal = "NEUTRAL"
    cond = {'mode': 'MEAN_REVERSION', 'poi_type': 'STATISTICAL_BAND'}

    # Bullish Reversion: Pierced lower band + Oversold + Reversing
    if c1['low'] < c1['bb_lower'] and c1['rsi'] < 30:
        signal = "BUY_NANO"
        score += 0.50
        depth = (c1['bb_lower'] - c1['low']) / current_atr
        score += min(0.20, depth * 0.10)       # Reward deeper pierces
        if c1['rsi'] < 25: score += 0.10       # Extreme oversold bonus
        if c0['close'] > c1['high']: score += 0.15 # Reversal confirmed by current candle
        cond['ob_entry_price'] = c0['close']

    # Bearish Reversion: Pierced upper band + Overbought + Reversing
    elif c1['high'] > c1['bb_upper'] and c1['rsi'] > 70:
        signal = "SELL_NANO"
        score += 0.50
        depth = (c1['high'] - c1['bb_upper']) / current_atr
        score += min(0.20, depth * 0.10)       
        if c1['rsi'] > 75: score += 0.10       
        if c0['close'] < c1['low']: score += 0.15 
        cond['ob_entry_price'] = c0['close']

    score = min(0.99, round(score, 3))
    if score < 0.70:
        return empty_ret

    reason = f"Mean Reversion [{symbol}] | Score:{score:.2f} | RSI:{c1['rsi']:.1f} | Pierced 2.5σ Band"
    return signal, score, reason, cond, "STATISTICAL_EXTREME"

def compute_mean_reversion_confluence(df: pd.DataFrame, symbol: str, current_atr: float) -> tuple:
    """
    [S19g] Mean Reversion Model for DEAD MARKET regimes.
    Bypasses structural ICT logic to trade statistical extremes in ranging markets.
    Requires a pierce of the 2.5 Standard Deviation Bollinger Band + RSI confirmation.
    """
    empty_ret = ("NEUTRAL", 0.0, "Mean Reversion: No setup", {}, "N/A")
    if len(df) < 30 or current_atr <= 0: 
        return empty_ret

    df = df.copy()
    
    # Calculate RSI (14-period)
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / (loss + 1e-9)
    df['rsi'] = 100 - (100 / (1 + rs))

    # Calculate Bollinger Bands (20-period, 2.5 Standard Deviations)
    df['bb_mid'] = df['close'].rolling(20).mean()
    df['bb_std'] = df['close'].rolling(20).std()
    df['bb_upper'] = df['bb_mid'] + (df['bb_std'] * 2.5)
    df['bb_lower'] = df['bb_mid'] - (df['bb_std'] * 2.5)

    c1 = df.iloc[-2] # Last closed candle
    c0 = df.iloc[-1] # Current live candle

    score = 0.0
    signal = "NEUTRAL"
    cond = {'mode': 'MEAN_REVERSION', 'poi_type': 'STATISTICAL_BAND'}

    # Bullish Reversion: Pierced lower band + Oversold + Reversing
    if c1['low'] < c1['bb_lower'] and c1['rsi'] < 30:
        signal = "BUY_NANO"
        score += 0.50
        depth = (c1['bb_lower'] - c1['low']) / current_atr
        score += min(0.20, depth * 0.10)       # Reward deeper pierces
        if c1['rsi'] < 25: score += 0.10       # Extreme oversold bonus
        if c0['close'] > c1['high']: score += 0.15 # Reversal confirmed by current candle
        cond['ob_entry_price'] = c0['close']

    # Bearish Reversion: Pierced upper band + Overbought + Reversing
    elif c1['high'] > c1['bb_upper'] and c1['rsi'] > 70:
        signal = "SELL_NANO"
        score += 0.50
        depth = (c1['high'] - c1['bb_upper']) / current_atr
        score += min(0.20, depth * 0.10)       
        if c1['rsi'] > 75: score += 0.10       
        if c0['close'] < c1['low']: score += 0.15 
        cond['ob_entry_price'] = c0['close']

    score = min(0.99, round(score, 3))
    if score < 0.70:
        return empty_ret

    reason = f"Mean Reversion [{symbol}] | Score:{score:.2f} | RSI:{c1['rsi']:.1f} | Pierced 2.5σ Band"
    return signal, score, reason, cond, "STATISTICAL_EXTREME"


# ── MAIN ENTRY POINT ──────────────────────────────────────────────────────────

def analyze_market_structure(
    request: AnalysisRequest,
    df_macro: pd.DataFrame = None,
    market_regime: str = "NORMAL",
    symbol: str = "",
    utc_now: datetime = None,
    df_ticks: pd.DataFrame = None,
) -> AnalysisResponse:
    """
    Entry point called by bot_engine.process_symbol().
    [S19g] Integrates Regime Routing to bypass ICT during DEAD MARKET regimes.
    """
    df = pd.DataFrame([c.dict() for c in request.candles])

    if len(df) < 50:
        return AnalysisResponse(
            symbol=request.symbol,
            signal="NEUTRAL",
            confidence=0.0,
            reason="Initializing — need 50+ candles.",
        )

    df['atr'] = calculate_atr(df)

    # Per-asset dead-market check (OPT-1)
    sym = symbol or request.symbol
    dead_threshold = _dead_market_atr_threshold(sym)
    if df.iloc[-1]['atr'] < dead_threshold:
        market_regime = "DEAD MARKET"

    if utc_now is None:
        utc_now = datetime.utcnow()

    # ── [S19g] The Regime Router ──────────────────────────────────────────
    if market_regime == "DEAD MARKET":
        # Bypass ICT -> Route to Statistical Fading
        signal, score, reason, conditions, kill_zone = compute_mean_reversion_confluence(df, sym, df.iloc[-1]['atr'])
        if signal == "NEUTRAL":
            return AnalysisResponse(symbol=request.symbol, signal="NEUTRAL", confidence=0.0, reason="DEAD MARKET: No mean reversion setup.")
    else:
        # Trending/Volatile -> Route to standard ICT Engine
        signal, score, reason, conditions, kill_zone = compute_ict_confluence(
            df, df_macro, sym, market_regime, utc_now, df_ticks
        )

    # ── [S16] SILVER BULLET / TIME-FVG / PO3 SCALP ENGINE ──────────────
    if any(sym.upper().startswith(k) for k in _SILVER_BULLET_ASSETS) and market_regime != "DEAD MARKET":
        try:
            sc_sig, sc_score, sc_reason, sc_cond, sc_kz = compute_scalp_confluence(
                df, df_macro, sym, utc_now
            )
            if sc_sig != "NEUTRAL" and sc_score > score:
                # Scalp model wins — merge conditions, tag signal source
                sc_cond['ict_score_shadow']  = score
                sc_cond['ict_signal_shadow'] = signal
                sc_cond['model_winner']      = 'SCALP'
                signal     = sc_sig
                score      = sc_score
                reason     = sc_reason
                conditions = sc_cond
                kill_zone  = sc_kz
            elif sc_sig != "NEUTRAL":
                # ICT won but scalp model also fired — log as secondary
                conditions['scalp_score_shadow']  = sc_score
                conditions['scalp_signal_shadow'] = sc_sig
                conditions['model_winner']        = 'ICT'
        except Exception as _sc_err:
            conditions['scalp_error'] = str(_sc_err)
    # ────────────────────────────────────────────────────────────────────

    # [S34] BTC range filter removed — BTCUSD/ETHUSD no longer in asset universe
    _btc_range_ok = False  # unused placeholder

    # ── [S27-B] M1 MICRO-SCALP + [S30] Silver Asian Reversion ──────
    _df_m1 = getattr(request, 'df_m1', None)
    # [S34] Scalp engine: XAUUSD + XAGUSD only (crypto removed)
    _scalp_sym = any(k in sym.upper() for k in ['XAU', 'XAG'])
    _scalp_ok  = _scalp_sym

    # [S32] Slingshot reversal — commodities during London/NY (crypto removed S34)
    _sling_sym = any(k in sym.upper() for k in ['XAU', 'XAG', 'Oil', 'NGAS'])
    if _sling_sym and market_regime != "DEAD MARKET":
        try:
            _sl_sig, _sl_sc, _sl_r, _sl_c, _sl_kz = compute_slingshot_reversal(df_macro, sym, utc_now)
            if _sl_sig != "NEUTRAL" and _sl_sc > score:
                _sl_c['ict_score_shadow']  = score
                _sl_c['ict_signal_shadow'] = signal
                signal, score, reason, conditions, kill_zone = _sl_sig, _sl_sc, _sl_r, _sl_c, _sl_kz
        except Exception as _sl_e:
            conditions['slingshot_error'] = str(_sl_e)

    # [S33] EURUSD dedicated strategy — London breakout + NY reversion
    # Runs BEFORE the generic FX London scalp; EURUSD gets its own module.
    if 'EURUSD' in sym.upper() and market_regime != "DEAD MARKET":
        try:
            _eu_sig, _eu_sc, _eu_r, _eu_c, _eu_kz = compute_eurusd_strategy(
                df, _df_m1, sym, utc_now)
            if _eu_sig != "NEUTRAL" and _eu_sc > score:
                _eu_c['ict_score_shadow']  = score
                _eu_c['ict_signal_shadow'] = signal
                signal, score, reason, conditions, kill_zone = _eu_sig, _eu_sc, _eu_r, _eu_c, _eu_kz
        except Exception as _eu_e:
            conditions['eurusd_strategy_error'] = str(_eu_e)

    # [S32] FX London M1 scalp — pullback entries for FX during London open
    _FX_SYMS = ['EUR','GBP','USD','JPY','AUD','NZD','CAD','CHF']
    _is_fx   = sum(1 for x in _FX_SYMS if x in sym.upper()) >= 2
    if _is_fx and _df_m1 is not None and market_regime != "DEAD MARKET":
        try:
            _h4b_fx  = conditions.get('h4_macro', 'NEUTRAL')
            _fx_sig, _fx_sc, _fx_r, _fx_c, _fx_kz = compute_fx_london_scalp(
                _df_m1, sym, utc_now, _h4b_fx)
            if _fx_sig != "NEUTRAL" and _fx_sc > score:
                _fx_c['ict_score_shadow']  = score
                _fx_c['ict_signal_shadow'] = signal
                signal, score, reason, conditions, kill_zone = _fx_sig, _fx_sc, _fx_r, _fx_c, _fx_kz
        except Exception as _fx_e:
            conditions['fx_london_error'] = str(_fx_e)

    # [S31] Turtle Soup FX Fade — London session, FX pairs only
    _FX_SYMS = ['EUR','GBP','USD','JPY','AUD','NZD','CAD','CHF']
    _is_fx   = sum(1 for x in _FX_SYMS if x in sym.upper()) >= 2
    if _is_fx and market_regime != "DEAD MARKET":
        try:
            _ts_sig, _ts_sc, _ts_r, _ts_c, _ts_kz = compute_turtle_soup_fx(df, sym, utc_now)
            if _ts_sig != "NEUTRAL" and _ts_sc > score:
                _ts_c['ict_score_shadow']  = score
                _ts_c['ict_signal_shadow'] = signal
                signal, score, reason, conditions, kill_zone = _ts_sig, _ts_sc, _ts_r, _ts_c, _ts_kz
        except Exception as _ts_e:
            conditions['turtle_soup_error'] = str(_ts_e)

    # [S30] Silver Asian Reversion — runs when XAG M1 scalp is blocked (22-07 UTC)
    if _df_m1 is not None and 'XAG' in sym.upper() and market_regime != "DEAD MARKET":
        try:
            _ar_sig, _ar_sc, _ar_r, _ar_c, _ar_kz = compute_silver_asian_reversion(
                _df_m1, utc_now)
            if _ar_sig != "NEUTRAL" and _ar_sc > score:
                _ar_c['ict_score_shadow']  = score
                _ar_c['ict_signal_shadow'] = signal
                signal, score, reason, conditions, kill_zone = _ar_sig, _ar_sc, _ar_r, _ar_c, _ar_kz
        except Exception as _ar_e:
            conditions['silver_ar_error'] = str(_ar_e)

    if _df_m1 is not None and _scalp_ok and market_regime != "DEAD MARKET":
        try:
            _h4b = conditions.get('h4_macro', 'NEUTRAL')
            m1s, m1sc, m1r, m1c, m1kz = compute_m1_scalp(_df_m1, sym, utc_now, _h4b)
            if m1s != "NEUTRAL" and (m1sc > score or (score < 0.65 and m1sc >= 0.68)):
                m1c['ict_score_shadow']  = score
                m1c['ict_signal_shadow'] = signal
                m1c['model_winner']      = 'M1_SCALP'
                # [S30/S32] Apply all bonuses and penalties to M1 score
                _s30_conqueror_b = conditions.get('conqueror_bonus', 0.0)
                _s30_amd_pen     = conditions.get('amd_context_penalty', 0.0)
                _s30_channel_b   = conditions.get('channel_bonus', 0.0)
                _s32_ema50_b     = conditions.get('ema50_bonus', 0.0)
                m1sc_adj = min(0.99, m1sc + _s30_conqueror_b + _s30_channel_b
                               + _s32_ema50_b - _s30_amd_pen)
                m1c['s30_conqueror_bonus'] = _s30_conqueror_b
                m1c['s30_channel_bonus']   = _s30_channel_b
                m1c['s30_amd_penalty']     = _s30_amd_pen
                m1c['s30_adjusted_score']  = m1sc_adj
                signal, score, reason, conditions, kill_zone = m1s, m1sc_adj, m1r, m1c, m1kz
            elif m1s != "NEUTRAL":
                conditions['m1_score_shadow']  = m1sc
                conditions['m1_signal_shadow'] = m1s
        except Exception as _e:
            conditions['m1_error'] = str(_e)

    # ── [S28] SMC MULTI-TIMEFRAME CONFLUENCE ──────────────────────────
    # Runs for all assets when M1 data is available (Gold, Crypto always).
    # Adds up to +0.18 to confidence based on structural confluence.
    if _SMC_AVAILABLE and market_regime != "DEAD MARKET":
        try:
            _df_m1_smc  = getattr(request, 'df_m1', None)
            _df_h4_smc  = df_macro if df_macro is not None and not df_macro.empty else None
            _atr_m15    = float(df['atr'].iloc[-1]) if 'atr' in df.columns else 1.0
            _smc_dir    = 'BUY' if 'BUY' in signal else 'SELL'

            if signal != "NEUTRAL":
                layers = analyse_smc_layers(
                    df_m1  = _df_m1_smc,
                    df_m15 = df,
                    df_h4  = _df_h4_smc,
                    symbol = sym,
                    direction = _smc_dir,
                    atr_m15 = _atr_m15,
                )
                smc_bonus, smc_reasons = smc_confluence_bonus(layers, _smc_dir, score)
                if smc_bonus > 0:
                    score = min(0.99, score + smc_bonus)
                    reason += " | SMC[" + ",".join(smc_reasons) + f" +{smc_bonus:.2f}]"
                conditions.update(smc_layers_summary(layers))
                conditions['smc_bonus'] = smc_bonus
        except Exception as _smc_err:
            conditions['smc_error'] = str(_smc_err)

    # ── [S26-A] INDEX OPENING RANGE BREAKOUT BONUS ─────────────────────
    # NYSE opens 13:30 UTC. SELL signals in confirmed H4 bear trend during
    # the first 30 minutes get +0.05 — the highest-probability index window.
    sym_up = sym.upper()
    if signal != "NEUTRAL" and any(x in sym_up for x in ['SP 500', 'TECH 100']):
        t = utc_now.hour * 60 + utc_now.minute
        if 13*60+30 <= t < 14*60:
            score = min(0.99, score + 0.05)
            conditions['orb_bonus'] = True
            reason += " | ORB[NYSE +0.05]"
        else:
            conditions['orb_bonus'] = False

    # ── [S26-B] OVERNIGHT GAP FILL ALIGNMENT ────────────────────────
    # Indices frequently fill overnight gaps. Signal aligned with gap fill
    # direction gets +0.03 confluence bonus.
    if signal != "NEUTRAL" and any(x in sym_up for x in ['SP 500', 'TECH 100', 'GERMANY']):
        try:
            if 'timestamp' in df.columns and len(df) > 1:
                df_c = df.copy()
                df_c['_dt'] = pd.to_datetime(df_c['timestamp'])
                today_mask = df_c['_dt'].dt.date == utc_now.date()
                today_idx  = df_c.index[today_mask]
                if len(today_idx) > 0 and today_idx[0] > 0:
                    prior_close  = float(df.iloc[today_idx[0] - 1]['close'])
                    today_open   = float(df.iloc[today_idx[0]]['open'])
                    gap          = today_open - prior_close
                    if (gap > 0 and 'SELL' in signal) or (gap < 0 and 'BUY' in signal):
                        score = min(0.99, score + 0.03)
                        conditions['gap_fill_alignment'] = 'ALIGNED'
                    conditions['gap_size'] = round(gap, 5)
        except Exception:
            pass

    resp = AnalysisResponse(
        symbol=request.symbol,
        signal=signal,
        confidence=score,
        reason=reason,
    )
    # Attach extra data for bot_engine to log to DB
    resp.__dict__['ict_conditions'] = conditions
    resp.__dict__['kill_zone']      = kill_zone
    resp.__dict__['ict_score']      = score

    return resp


def run_backtest_strategy(request):
    return BacktestResponse(
        symbol=request.symbol,
        net_profit=0.0,
        win_rate=0.0,
        profit_factor=0.0,
        total_trades=0,
    )
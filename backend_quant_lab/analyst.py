# ============================================================
# TradeCore v53.0 — analyst.py  [SPRINT 13 CONFLUENCE AMPLIFIER]
#
# SPRINT 13 ADDITIONS — MULTI-FRAMEWORK CONFLUENCE LAYER:
#   [S13-F1] compute_vwap_context()
#              VWAP + Z-score (|Z|>1.5 = extreme) + slope.
#              Fixes: stale H4 macro trend blocked USDJPY 0.66 for 2h.
#              VWAP slope now overrides H4 EMA as intraday bias in DISTRIBUTION.
#              Score: +0.10 at VWAP extreme in signal direction.
#
#   [S13-F2] compute_volume_profile()
#              POC / VAH / VAL / HVN / LVN from M15 last-200-bar window.
#              Fixes: OB:True/False binary — no zone quality grading.
#              Germany 40 5.1× vol at FVG scored 0.55 for 80 min; HVN fix.
#              Score: +0.12 OB at HVN | +0.08 FVG in LVN.
#
#   [S13-F3] compute_delta_context()
#              Proxy cumulative delta (bull/bear candle × volume).
#              Fixes: 5× buy vol and 5× sell vol treated identically.
#              Score: +0.08 delta confirms direction | +0.06 divergence.
#
#   [S13-F4] wyckoff_spring_check()
#              Low-vol test at sweep candle = genuine Spring / Upthrust.
#              Fixes: AMD MANIPULATION confirmed late (sweep+disp = 2 candles).
#              Wyckoff: vol at sweep < 70% session avg = exhausted sellers.
#              Score: +0.07 Spring (BUY) | +0.07 Upthrust (SELL).
#
#   Max S13 bonus: +0.61 | Score ceiling: 0.99 | Thresholds unchanged (0.80/0.90)
#   Integration: additive bonuses in BULLISH MANIP, BEARISH MANIP, DISTRIBUTION
#   Architecture: ICT core generates signal, S13 layer amplifies confidence.
#
# SPRINT 13 ALSO INCLUDES:
#   VWAP macro override: in DISTRIBUTION, VWAP slope replaces stale H4 EMA
#   when the two conflict. Prevents blocking live distribution signals with
#   4-hour-lagged trend filters.
#
# SPRINT 12 ADDITIONS — ENTRY/EXIT PRECISION:
#   [S12-P0A] Structural entry: OB body_low/high (not live candle price)
#   [S12-P0B] Structural SL: swing_sl_ref (swept level) + 0.3×ATR buffer
#   [S12-P1B] Liquidity-pool TP: Asian High (bull) / Asian Low (bear)
#   [S12-AMD-B] Judas bonus quality-scaled by accum confidence
#   [S12-AMD-C] Session-bounded AMD lookback
#   [S12-AMD-D] INDETERMINATE phase for trending/ambiguous markets
#   [S12-AMD-E] Index-aware AMD priors (US indices vs DAX vs FX calendar)
#   [S12-AMD-F] Gold/Silver ATR buffer: max(0.5×ATR, 0.50)
#
# SPRINT 9 ADDITIONS — AMD PHASE AWARENESS:
#   [AMD-1] detect_amd_phase() — maps UTC hour to market cycle phase
#            ACCUMULATION | MANIPULATION | NY_MANIPULATION | DISTRIBUTION | AVOID
#   [AMD-2] detect_asian_range() — computes today's Asian High/Low (00:00-03:00 UTC)
#            AH and AL are the structural reference levels for the Judas Swing
#   [AMD-3] detect_judas_swing() — confirms sweep specifically targets AH or AL
#            Judas Swing is the highest-probability ICT daily setup
#   [AMD-4] detect_london_range() — tracks London session High/Low for NY Judas Swing
#   [AMD-5] Phase-gated scoring in compute_ict_confluence():
#            ACCUMULATION   → NEUTRAL always. Engine maps range, does not trade.
#            MANIPULATION   → Full sweep+displacement scoring + Judas Swing bonus
#            NY_MANIPULATION→ Same but targets London range (LH/LL) instead of AH/AL
#            DISTRIBUTION   → Continuation scoring: OB/FVG retest in direction of
#                             manipulation move. Sweep gate relaxed (CHoCH valid).
#            AVOID          → NEUTRAL (NY Lunch equivalent)
#
# SPRINT 9 PRECISION FIXES (retained):
#   [S9-FIX-1] Sweep: hybrid tier (structural pivot → rolling fallback)
#   [S9-FIX-2] Displacement: 0.6 ATR body + 70% close quality (M15-calibrated)
#   [S9-FIX-3] Volume: 1.3x threshold (empirically calibrated from live data)
#   [S9-FIX-4] OB: body >= 0.5 ATR, freshness guard (< 15 bars), in-zone retest
#   [S9-FIX-5] FVG: 10-bar scan, >= 0.3 ATR gap, unfilled validation
#   [S9-FIX-6] BOS: 3-swing confirmation
#   [S9-FIX-7] P/D: 100-bar lookback, deep zone scoring, H4 conflict check
#
# SCORE ARCHITECTURE:
#   MANIPULATION MODE (primary):
#     Sweep           +0.20  (mandatory gate)
#     Displacement    +0.15  (mandatory gate)
#     Kill Zone       +0.15 × session_weight
#     Order Block     +0.15
#     FVG             +0.10
#     Premium/Discount+0.10 (deep) / +0.05 (shallow)
#     BOS aligned     +0.08
#     OTE zone        +0.07
#     Judas Swing     +0.12  (bonus: sweep targets AH/AL or LH/LL specifically)
#     Volume surge    ×1.10  (multiplier, capped 0.99)
#
#   DISTRIBUTION MODE (continuation):
#     OB retest       +0.30  (primary signal — price returns to manipulation OB)
#     FVG retest      +0.20  (imbalance fill in trend direction)
#     BOS confirmed   +0.15  (structure confirms continuation)
#     Kill Zone       +0.15 × session_weight
#     OTE zone        +0.10  (optimal pullback depth)
#     Volume surge    ×1.10  (multiplier)
#     NOTE: Sweep not required. Entry is on RETRACEMENT, not new sweep.
#
#   Execution threshold = 0.80 standard / 0.90 sniper (bot_engine.py)
# ============================================================

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

def _dead_market_atr_threshold(symbol: str) -> float:
    s = symbol.upper()
    if "BTC" in s:  return 50.0
    if "ETH" in s:  return 5.0
    if "XAU" in s:  return 0.30
    if "XAG" in s:  return 0.05
    if "SP 500" in s or "NAS" in s or "Tech 100" in s: return 2.0
    if "JPY" in s:  return 0.030
    return 0.0003



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

    # NY PM (14:30-16:00 UTC): NY distribution
    return {'DISTRIBUTION': 1.2, 'MANIPULATION': 0.9, 'ACCUMULATION': 0.8}


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


# ── CORE ICT CONFLUENCE SCORER ────────────────────────────────────────────────

def compute_ict_confluence(df: pd.DataFrame, df_macro: pd.DataFrame,
                            symbol: str, market_regime: str,
                            utc_now: datetime = None) -> tuple:
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

    # ── ACCUMULATION — map range, do not trade ─────────────────────
    if amd_phase == 'ACCUMULATION':
        cond = {
            'mode': 'ACCUMULATION', 'amd_phase': amd_phase,
            'amd_confidence': amd_conf, 'clock_label': clock_label,
            'asian_high':  asian.get('asian_high'),
            'asian_low':   asian.get('asian_low'),
            'range_high':  accum_data.get('range_high'),
            'range_low':   accum_data.get('range_low'),
            'range_atr':   accum_data.get('range_atr'),
        }
        reason = (f"ACCUMULATION [{clock_label}] | conf:{amd_conf:.2f} | "
                  f"Mapping range | AH:{asian.get('asian_high')} "
                  f"AL:{asian.get('asian_low')}")
        return "NEUTRAL", 0.0, reason, cond, clock_label

    # ── INDETERMINATE — trending/ambiguous market, no clean phase ──
    # [S12-AMD-D] When all three phase detectors fall below their thresholds,
    # the market is moving without a readable AMD structure. Do not trade:
    # boundaries are unreliable and the manipulation detector would fire
    # on noise rather than genuine stop-hunt sweeps.
    if amd_phase == 'INDETERMINATE':
        reason = (f"INDETERMINATE [{clock_label}] | conf:{amd_conf:.2f} | "
                  f"Price trending, no clean AMD range — skipping.")
        return "NEUTRAL", 0.0, reason, {'mode': 'INDETERMINATE', 'amd_confidence': amd_conf,
                                         'clock_label': clock_label}, clock_label

    # ── AVOID ──────────────────────────────────────────────────────
    if amd_phase == 'AVOID':
        return "NEUTRAL", 0.0, "NY Lunch: Reaccumulation. Skipped.", {}, "NY_Lunch"

    # ── Session weight (kill zone quality) ─────────────────────────
    session_wt, kill_zone = get_ict_session_weight(utc_now.hour, utc_now.minute)
    if kill_zone == 'NY_Lunch':
        return "NEUTRAL", 0.0, "NY Lunch: Low-quality session. Skipped.", {}, kill_zone

    if "DEAD MARKET" in market_regime:
        disp_atr_mult = 0.3;  enforce_macro = False
    elif "HIGH VOLATILITY" in market_regime:
        disp_atr_mult = 0.8;  enforce_macro = True
    else:
        disp_atr_mult = 0.6;  enforce_macro = False

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

            if "DEAD MARKET" in market_regime:
                signal = "BUY_NANO" if "BUY" in signal else "SELL_NANO"
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

        if s13_bonus > 0:
            score = min(0.99, score + s13_bonus)

        # ── Signal type ─────────────────────────────────────────────
        if "DEAD MARKET" in market_regime:       signal = "BUY_NANO"
        elif "HIGH VOLATILITY" in market_regime: signal = "BUY"
        else:                                    signal = "BUY_MICRO"

        tag = f" ⚡JUDAS({ref_label})" if is_judas else ""
        s13_tag = (f" | S13[Z:{s13_vwap.get('vwap_z',0):.2f}"
                   f" HVN:{ob_at_hvn} Δ:{s13_delta.get('cum_delta_slope',0):.0f}"
                   f" Spring:{spring_ok}]") if s13_bonus > 0 else ""
        reason = (f"ICT Bullish [{kill_zone}]{tag} | AMD:{amd_phase} | "
                  f"Score:{score:.2f} | OB:{ob_hit} FVG:{fvg_bull} "
                  f"Disc:{in_discount}(deep:{deep_pd}) BOS:{bos_aligned} "
                  f"OTE:{ote_hit} Vol:{vol_ratio:.1f}x{s13_tag}")
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
        # Swept the Asian High (BSL cleared) → target the Asian Low (SSL below).
        cond['tp_target_level'] = asian.get('asian_low') or ref_low
        cond['asian_high']      = asian.get('asian_high')
        cond['asian_low']       = asian.get('asian_low')

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

        if s13_bonus > 0:
            score = min(0.99, score + s13_bonus)

        # ── Signal type ─────────────────────────────────────────────
        if "DEAD MARKET" in market_regime:       signal = "SELL_NANO"
        elif "HIGH VOLATILITY" in market_regime: signal = "SELL"
        else:                                    signal = "SELL_MICRO"

        tag = f" ⚡JUDAS({ref_label})" if is_judas else ""
        s13_tag = (f" | S13[Z:{s13_vwap.get('vwap_z',0):.2f}"
                   f" HVN:{ob_at_hvn} Δ:{s13_delta.get('cum_delta_slope',0):.0f}"
                   f" Upthrust:{upthrust_ok}]") if s13_bonus > 0 else ""
        reason = (f"ICT Bearish [{kill_zone}]{tag} | AMD:{amd_phase} | "
                  f"Score:{score:.2f} | OB:{ob_hit} FVG:{fvg_bear} "
                  f"Prem:{in_premium}(deep:{deep_pd}) BOS:{bos_aligned} "
                  f"OTE:{ote_hit} Vol:{vol_ratio:.1f}x{s13_tag}")
        return signal, round(score, 3), reason, cond, kill_zone

    return "NEUTRAL", 0.0, f"[{amd_phase}] No sweep or displacement detected.", {}, kill_zone
    if utc_now is None:
        utc_now = datetime.utcnow()

    if len(df) < 50:
        return "NEUTRAL", 0.0, "Gathering Data", {}, "N/A"

    # ── Pre-compute indicators ──────────────────────────────────────
    df = df.copy()
    df['atr']        = calculate_atr(df)
    df['avg_volume'] = df['volume'].rolling(20).mean()

    c1 = df.iloc[-3]   # sweep candle
    c2 = df.iloc[-2]   # displacement candle
    c3 = df.iloc[-1]   # current / entry candle

    current_atr = max(df['atr'].iloc[-1], 1e-8)

    # Volume on sweep and displacement candles
    avg_vol     = df['avg_volume'].iloc[-1]
    vol_c1      = c1['volume'] / avg_vol if avg_vol > 0 else 1.0
    vol_c2      = c2['volume'] / avg_vol if avg_vol > 0 else 1.0
    # [S9-FIX] Take max of both candles. Threshold lowered 1.5x → 1.3x.
    # Diagnosis: GBPJPY/EURJPY best setups of the day (0.850) had vol 1.2–1.3x
    # and were blocked entirely. 1.3x is the realistic M15 institutional volume
    # surge threshold — still meaningfully above average, not noise.
    vol_ratio   = max(vol_c1, vol_c2)

    # ── Regime multipliers ─────────────────────────────────────────
    if "DEAD MARKET" in market_regime:
        disp_atr_mult = 0.3
        enforce_macro = False
    elif "HIGH VOLATILITY" in market_regime:
        disp_atr_mult = 0.8
        enforce_macro = True
    else:
        # [S9-FIX] Normal regime: 1.0 → 0.6 ATR.
        # 1.0 ATR on M15 = 15–20 pip body — too large for real London setups.
        # 0.6 ATR ≈ 9–12 pips: genuine institutional displacement on M15.
        disp_atr_mult = 0.6
        enforce_macro = False

    # ── Session weight ─────────────────────────────────────────────
    session_wt, kill_zone = get_ict_session_weight(utc_now.hour, utc_now.minute)
    if kill_zone == 'NY_Lunch':
        return "NEUTRAL", 0.0, "NY Lunch: Low-quality session. Skipped.", {}, kill_zone

    # ── Market structure (provides swing points for sweep + BOS) ───
    structure   = detect_market_structure(df)
    h4_trend    = _derive_macro_trend(df_macro)
    macro_trend = h4_trend

    # ── Order blocks ───────────────────────────────────────────────
    obs = detect_order_blocks(df)

    # ── Premium/Discount — 100-bar lookback, depth scoring, H4 context
    pd_zone = detect_premium_discount(df, df_macro=df_macro, lookback=100)

    # ── Equal Highs/Lows — engineered liquidity ────────────────────
    eq_levels = detect_equal_highs_lows(df)

    # ── SWEEP DETECTION ────────────────────────────────────────────
    # [S9-FIX] Hybrid two-tier approach to prevent engine silence.
    #
    # Diagnosis: the pure swing-structure gate caused complete silence after
    # ~5 hours because detect_market_structure() requires 30+ bars with pivot
    # geometry on both sides of each point. In ranging/trending markets the
    # swing_lows list becomes empty, the fallback used the current bar's own
    # rolling min (not a swept level), and the depth guard blocked everything.
    # London Open produced ZERO signals all day as a result.
    #
    # FIX — Tier 1 (structural, preferred): use confirmed swing low/high IF
    #   >= 2 swing points exist. Depth guard relaxed 0.5 → 0.3 ATR.
    #   A 0.3 ATR wick below a structural pivot is a meaningful sweep.
    # FIX — Tier 2 (rolling fallback): if swing points insufficient, use
    #   rolling 20-bar low/high with depth >= 0.5 ATR. This is stricter on
    #   depth to compensate for the less precise reference level.
    # Both tiers still reject micro-wicks and require genuine penetration.

    swing_lows  = structure.get('swing_lows', [])
    swing_highs = structure.get('swing_highs', [])

    if len(swing_lows) >= 2:
        # Tier 1: confirmed structural pivot
        last_swing_low   = swing_lows[-1][1]
        sweep_depth_bull = last_swing_low - c1['low']
        sweep_low        = (c1['low'] < last_swing_low and
                            sweep_depth_bull >= current_atr * 0.3)
        sweep_tier       = 'structural'
    else:
        # Tier 2: rolling reference with tighter depth guard
        last_swing_low   = df['low'].rolling(20).min().shift(1).iloc[-1]
        sweep_depth_bull = last_swing_low - c1['low']
        sweep_low        = (c1['low'] < last_swing_low and
                            sweep_depth_bull >= current_atr * 0.5)
        sweep_tier       = 'rolling'

    if len(swing_highs) >= 2:
        last_swing_high   = swing_highs[-1][1]
        sweep_depth_bear  = c1['high'] - last_swing_high
        sweep_high        = (c1['high'] > last_swing_high and
                             sweep_depth_bear >= current_atr * 0.3)
    else:
        last_swing_high   = df['high'].rolling(20).max().shift(1).iloc[-1]
        sweep_depth_bear  = c1['high'] - last_swing_high
        sweep_high        = (c1['high'] > last_swing_high and
                             sweep_depth_bear >= current_atr * 0.5)

    # Equal-lows/highs bonus: sweep targeting a liquidity cluster is higher quality
    eq_low_sweep  = sweep_low  and any(abs(last_swing_low  - lvl) < current_atr * 0.3
                                       for lvl in eq_levels.get('equal_lows', []))
    eq_high_sweep = sweep_high and any(abs(last_swing_high - lvl) < current_atr * 0.3
                                       for lvl in eq_levels.get('equal_highs', []))

    # ── DISPLACEMENT DETECTION ─────────────────────────────────────
    # [S9-FIX] Displacement threshold calibrated to M15 reality.
    #
    # Diagnosis: 1.0 ATR body on M15 = 15–20 pip candle on EURUSD.
    # London Open typically produces 8–12 pip displacement candles — genuine
    # institutional commitment but below the 1.0 ATR bar. Combined with the
    # broken sweep gate, zero setups reached displacement check at all.
    #
    # FIX — Normal regime: 1.0 → 0.6 ATR body minimum.
    #   0.6 ATR ≈ 9–12 pips on EURUSD / 6–9 pips on EURGBP.
    #   This is the empirically correct threshold for M15 institutional moves.
    # FIX — Close quality: 75% → 70% (candle closes in top/bottom 30% of range).
    #   Still eliminates doji candles and indecision bars.
    #   A strong displacement candle closing at 70% of its range is valid.
    body_bull  = abs(c2['close'] - c2['open'])
    body_bear  = abs(c2['open']  - c2['close'])
    c2_range   = c2['high'] - c2['low']

    close_quality_bull = ((c2['close'] - c2['low']) / c2_range) if c2_range > 0 else 0
    close_quality_bear = ((c2['high'] - c2['close']) / c2_range) if c2_range > 0 else 0

    disp_up   = (c2['close'] > c2['open'] and
                 body_bull >= current_atr * disp_atr_mult and
                 close_quality_bull >= 0.70)   # closes in top 30% of range

    disp_down = (c2['close'] < c2['open'] and
                 body_bear >= current_atr * disp_atr_mult and
                 close_quality_bear >= 0.70)   # closes in bottom 30% of range

    # ── BULLISH SCORING ────────────────────────────────────────────
    if sweep_low and disp_up:
        score = 0.0
        cond  = {}

        cond['sweep']        = True;  score += 0.20
        cond['displacement'] = True;  score += 0.15

        # Kill zone contribution
        zone_contrib = 0.15 * session_wt
        cond['kill_zone'] = kill_zone
        score += zone_contrib

        # Order Block: active retest of fresh OB zone
        bull_ob = obs.get('bullish')
        ob_hit  = bool(bull_ob and bull_ob.get('retested'))
        cond['order_block'] = ob_hit
        if ob_hit: score += 0.15

        # FVG: unfilled bullish imbalance >= 0.3 ATR
        fvg_bull = detect_fvg(df, 'BUY', current_atr)
        cond['fvg'] = fvg_bull
        if fvg_bull: score += 0.10

        # Premium/Discount — deep discount scores full 0.10, shallow 0.05
        in_discount = pd_zone.get('in_discount', False)
        deep_pd     = pd_zone.get('deep', False) and in_discount
        h4_conflict = pd_zone.get('h4_conflict', False)
        cond['discount_zone'] = in_discount
        cond['deep_discount'] = deep_pd
        if deep_pd and not h4_conflict:
            score += 0.10
        elif in_discount and not h4_conflict:
            score += 0.05

        # BOS: 3-swing confirmed bullish structure
        bos_aligned = structure['trend'] == 'BULLISH'
        cond['bos_aligned'] = bos_aligned
        if bos_aligned: score += 0.08

        # OTE: measured from sweep low → displacement high (actual impulse leg)
        imp_low_ote  = c1['low']   # sweep candle low = origin of impulse
        imp_high_ote = c2['high']  # displacement candle high = end of impulse
        ote_low, ote_high = get_ote_zone(imp_high_ote, imp_low_ote, 'BUY')
        ote_hit = (ote_low > 0 and ote_low <= c3['close'] <= ote_high)
        cond['ote'] = ote_hit
        if ote_hit: score += 0.07

        # Volume: both sweep and displacement show >= 1.5x average
        vol_surge = vol_ratio >= 1.3
        cond['volume_surge'] = vol_surge
        cond['vol_ratio']    = round(vol_ratio, 2)
        if vol_surge:
            score = min(0.99, score * 1.10)

        # Equal-lows sweep bonus: embedded in signal quality note, no extra points
        cond['eq_lows_sweep'] = eq_low_sweep

        # Macro filter
        if enforce_macro and macro_trend == "BEARISH":
            reason = f"BUY blocked by Bearish H4 in HIGH VOL regime (score={score:.2f})"
            return "NEUTRAL", 0.0, reason, cond, kill_zone

        if "DEAD MARKET" in market_regime:
            signal = "BUY_NANO"
        elif "HIGH VOLATILITY" in market_regime:
            signal = "BUY"
        else:
            signal = "BUY_MICRO"

        reason = (f"ICT Bullish [{kill_zone}] | Score:{score:.2f} | "
                  f"OB:{ob_hit} FVG:{fvg_bull} Disc:{in_discount}(deep:{deep_pd}) "
                  f"BOS:{bos_aligned} OTE:{ote_hit} Vol:{vol_ratio:.1f}x "
                  f"EqLow:{eq_low_sweep}")
        return signal, round(score, 3), reason, cond, kill_zone

    # ── BEARISH SCORING ────────────────────────────────────────────
    if sweep_high and disp_down:
        score = 0.0
        cond  = {}

        cond['sweep']        = True;  score += 0.20
        cond['displacement'] = True;  score += 0.15

        zone_contrib = 0.15 * session_wt
        cond['kill_zone'] = kill_zone
        score += zone_contrib

        bear_ob = obs.get('bearish')
        ob_hit  = bool(bear_ob and bear_ob.get('retested'))
        cond['order_block'] = ob_hit
        if ob_hit: score += 0.15

        fvg_bear = detect_fvg(df, 'SELL', current_atr)
        cond['fvg'] = fvg_bear
        if fvg_bear: score += 0.10

        in_premium = pd_zone.get('in_premium', False)
        deep_pd    = pd_zone.get('deep', False) and in_premium
        h4_conflict = pd_zone.get('h4_conflict', False)
        cond['premium_zone'] = in_premium
        cond['deep_premium'] = deep_pd
        if deep_pd and not h4_conflict:
            score += 0.10
        elif in_premium and not h4_conflict:
            score += 0.05

        bos_aligned = structure['trend'] == 'BEARISH'
        cond['bos_aligned'] = bos_aligned
        if bos_aligned: score += 0.08

        imp_high_ote = c1['high']  # sweep candle high = origin of impulse
        imp_low_ote  = c2['low']   # displacement candle low = end of impulse
        ote_low, ote_high = get_ote_zone(imp_high_ote, imp_low_ote, 'SELL')
        ote_hit = (ote_low > 0 and ote_low <= c3['close'] <= ote_high)
        cond['ote'] = ote_hit
        if ote_hit: score += 0.07

        vol_surge = vol_ratio >= 1.3
        cond['volume_surge'] = vol_surge
        cond['vol_ratio']    = round(vol_ratio, 2)
        if vol_surge:
            score = min(0.99, score * 1.10)

        cond['eq_highs_sweep'] = eq_high_sweep

        if enforce_macro and macro_trend == "BULLISH":
            reason = f"SELL blocked by Bullish H4 in HIGH VOL regime (score={score:.2f})"
            return "NEUTRAL", 0.0, reason, cond, kill_zone

        if "DEAD MARKET" in market_regime:
            signal = "SELL_NANO"
        elif "HIGH VOLATILITY" in market_regime:
            signal = "SELL"
        else:
            signal = "SELL_MICRO"

        reason = (f"ICT Bearish [{kill_zone}] | Score:{score:.2f} | "
                  f"OB:{ob_hit} FVG:{fvg_bear} Prem:{in_premium}(deep:{deep_pd}) "
                  f"BOS:{bos_aligned} OTE:{ote_hit} Vol:{vol_ratio:.1f}x "
                  f"EqHigh:{eq_high_sweep}")
        return signal, round(score, 3), reason, cond, kill_zone

    return "NEUTRAL", 0.0, "No sweep or displacement detected.", {}, kill_zone


# ── MAIN ENTRY POINT ──────────────────────────────────────────────────────────

def analyze_market_structure(
    request: AnalysisRequest,
    df_macro: pd.DataFrame = None,
    market_regime: str = "NORMAL",
    symbol: str = "",
    utc_now: datetime = None,
) -> AnalysisResponse:
    """
    Entry point called by bot_engine.process_symbol().
    Returns AnalysisResponse with signal, confidence (ICT score), reason.
    Also returns conditions dict attached as extra attribute for DB logging.
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

    signal, score, reason, conditions, kill_zone = compute_ict_confluence(
        df, df_macro, sym, market_regime, utc_now
    )

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

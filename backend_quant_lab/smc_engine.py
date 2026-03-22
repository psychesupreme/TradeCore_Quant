# ============================================================
# Kom v1.0 — smc_engine.py
# [SPRINT 28] Advanced ICT + Smart Money Concepts Engine
#
# Multi-timeframe structure detection:
#   - FVG classification: Nano / Micro / Normal / Macro
#   - Inverse FVG (IFVG): filled FVGs acting as S/R
#   - OB classification: Nano / Micro / Normal / Macro
#   - Breaker Blocks: failed OBs flipped to magnets
#   - Mitigation Blocks: partially-filled OBs
#   - Liquidity Voids: large price inefficiencies
#   - Market Structure Breaks (MSB / MSS)
#   - Multi-TF confluence scorer
# ============================================================

import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from typing import Optional

# ── DATA CLASSES ────────────────────────────────────────────

@dataclass
class FVGLevel:
    """A Fair Value Gap with tier classification."""
    tier:       str     # 'nano' | 'micro' | 'normal' | 'macro'
    direction:  str     # 'BUY' | 'SELL'
    high:       float   # top of the gap
    low:        float   # bottom of the gap
    mid:        float   # midpoint (entry zone)
    size:       float   # gap size in price points
    bar_index:  int     # bar where the gap was created
    filled:     bool    # whether gap has been fully filled
    fill_pct:   float   # 0.0 = untouched, 1.0 = fully filled
    is_ifvg:    bool    # True when this is a flipped (Inverse) FVG
    atr_mult:   float   # size as multiple of ATR at creation


@dataclass
class OrderBlock:
    """An Order Block with tier and mitigation tracking."""
    tier:        str     # 'nano' | 'micro' | 'normal' | 'macro'
    direction:   str     # 'BUY' | 'SELL'
    body_high:   float
    body_low:    float
    wick_high:   float
    wick_low:    float
    bar_index:   int
    mitigated:   bool    # True if price has entered the OB zone
    mitig_pct:   float   # 0.0 = untouched, 1.0 = fully consumed
    is_breaker:  bool    # True if this OB has been swept and flipped


@dataclass
class LiquidityVoid:
    """A large price inefficiency (extended FVG / liquidity void)."""
    direction:  str
    high:       float
    low:        float
    size:       float
    bar_index:  int
    atr_mult:   float   # typically > 3.0


@dataclass
class MarketStructureBreak:
    """A Break of Structure event."""
    direction:  str     # 'BUL' (bullish break) | 'BEA' (bearish break)
    strength:   str     # 'strong' | 'weak'
    is_mss:     bool    # Market Structure Shift (first BOS after swing)
    price:      float   # price at which structure broke
    bar_index:  int
    atr_at_break: float


@dataclass
class SMCLayers:
    """
    Complete multi-timeframe SMC analysis result.
    Populated by analyse_smc_layers() and attached to analysis conditions.
    """
    # FVGs by tier
    fvg_nano:    list = field(default_factory=list)   # M1
    fvg_micro:   list = field(default_factory=list)   # M5
    fvg_normal:  list = field(default_factory=list)   # M15 (existing)
    fvg_macro:   list = field(default_factory=list)   # H4
    fvg_ifvg:    list = field(default_factory=list)   # All IFVGs

    # OBs by tier
    ob_nano:     list = field(default_factory=list)
    ob_micro:    list = field(default_factory=list)
    ob_normal:   list = field(default_factory=list)
    ob_macro:    list = field(default_factory=list)
    ob_breaker:  list = field(default_factory=list)   # Breaker Blocks

    # Liquidity Voids
    liq_voids:   list = field(default_factory=list)

    # Market Structure Breaks
    msb_list:    list = field(default_factory=list)

    # Confluence metrics
    stack_score:     float = 0.0   # multi-TF FVG stack at same price level
    structure_score: float = 0.0   # overall structural quality 0-1
    nearest_fvg:     Optional[FVGLevel] = None
    nearest_ob:      Optional[OrderBlock] = None
    nearest_void:    Optional[LiquidityVoid] = None
    active_breaker:  Optional[OrderBlock] = None
    last_msb:        Optional[MarketStructureBreak] = None


# ── FVG DETECTION ───────────────────────────────────────────

def _classify_fvg_tier(size: float, atr: float) -> str:
    """Classify an FVG by size relative to ATR."""
    if atr <= 0:
        return 'normal'
    ratio = size / atr
    if ratio < 0.5:   return 'nano'
    if ratio < 1.0:   return 'micro'
    if ratio < 2.5:   return 'normal'
    return 'macro'


def detect_fvgs_tiered(df: pd.DataFrame, atr: float,
                        direction: str, lookback: int = 50) -> list:
    """
    Detect FVGs (Fair Value Gaps) across the last `lookback` bars.
    A bullish FVG: candle[i-1].high < candle[i+1].low (gap up — price imbalance)
    A bearish FVG: candle[i-1].low  > candle[i+1].high (gap down — price imbalance)

    Returns list of FVGLevel objects, most recent first.
    Marks any previously detected FVG that has been filled as is_ifvg.
    """
    if len(df) < 3:
        return []

    n       = len(df)
    start   = max(1, n - lookback - 1)
    price   = float(df.iloc[-1]['close'])
    results = []

    for i in range(start, n - 1):
        try:
            h_prev = float(df.iloc[i-1]['high'])
            l_prev = float(df.iloc[i-1]['low'])
            h_next = float(df.iloc[i+1]['high'])
            l_next = float(df.iloc[i+1]['low'])
        except (IndexError, KeyError):
            continue

        if direction == 'BUY':
            # Bullish FVG: gap between prev high and next low
            if l_next > h_prev:
                gap_lo, gap_hi = h_prev, l_next
                gap_size = gap_hi - gap_lo
                tier     = _classify_fvg_tier(gap_size, atr)
                fill_pct = max(0.0, min(1.0, (price - gap_lo) / gap_size)) if gap_size else 0.0
                filled   = fill_pct >= 0.95
                results.append(FVGLevel(
                    tier=tier, direction='BUY',
                    high=round(gap_hi, 5), low=round(gap_lo, 5),
                    mid=round((gap_hi + gap_lo) / 2, 5),
                    size=round(gap_size, 5), bar_index=i,
                    filled=filled, fill_pct=round(fill_pct, 3),
                    is_ifvg=filled,  # once filled, becomes IFVG
                    atr_mult=round(gap_size / atr, 2) if atr else 0.0,
                ))
        else:
            # Bearish FVG: gap between next high and prev low
            if h_next < l_prev:
                gap_lo, gap_hi = h_next, l_prev
                gap_size = gap_hi - gap_lo
                tier     = _classify_fvg_tier(gap_size, atr)
                fill_pct = max(0.0, min(1.0, (gap_hi - price) / gap_size)) if gap_size else 0.0
                filled   = fill_pct >= 0.95
                results.append(FVGLevel(
                    tier=tier, direction='SELL',
                    high=round(gap_hi, 5), low=round(gap_lo, 5),
                    mid=round((gap_hi + gap_lo) / 2, 5),
                    size=round(gap_size, 5), bar_index=i,
                    filled=filled, fill_pct=round(fill_pct, 3),
                    is_ifvg=filled,
                    atr_mult=round(gap_size / atr, 2) if atr else 0.0,
                ))

    # Sort most recent first
    results.sort(key=lambda x: x.bar_index, reverse=True)
    return results


# ── ORDER BLOCK DETECTION ────────────────────────────────────

def _classify_ob_tier(candle_body: float, atr: float) -> str:
    """Classify OB tier by body size vs ATR."""
    if atr <= 0:
        return 'normal'
    r = candle_body / atr
    if r < 0.5:  return 'nano'
    if r < 1.0:  return 'micro'
    if r < 2.5:  return 'normal'
    return 'macro'


def detect_obs_tiered(df: pd.DataFrame, atr: float,
                       direction: str, lookback: int = 40) -> list:
    """
    Detect Order Blocks with tier classification and mitigation tracking.

    Bullish OB: last down-close candle before a strong up-move
    Bearish OB: last up-close candle before a strong down-move

    Also detects Breaker Blocks: OBs that price has swept through entirely.
    Returns list of OrderBlock objects, most recent first.
    """
    if len(df) < 4:
        return []

    n      = len(df)
    start  = max(1, n - lookback)
    price  = float(df.iloc[-1]['close'])
    min_move = atr * 0.8   # minimum displacement to qualify OB
    results  = []

    for i in range(start, n - 2):
        try:
            o   = float(df.iloc[i]['open'])
            c   = float(df.iloc[i]['close'])
            hi  = float(df.iloc[i]['high'])
            lo  = float(df.iloc[i]['low'])
            body = abs(c - o)

            # Next candle displacement (confirms the OB)
            c_next = float(df.iloc[i+1]['close'])
            displacement = abs(c_next - c)
            if displacement < min_move:
                continue

            if direction == 'BUY' and c < o:
                # Bearish candle before bullish move → Bullish OB
                tier       = _classify_ob_tier(body, atr)
                body_lo, body_hi = c, o
                # Mitigation: how much has price re-entered the OB?
                entry_depth = max(0.0, price - body_lo) / body if body else 0.0
                mitig_pct   = min(1.0, entry_depth)
                mitigated   = mitig_pct >= 0.50
                is_breaker  = price < body_lo  # price swept below OB entirely
                results.append(OrderBlock(
                    tier=tier, direction='BUY',
                    body_high=round(body_hi, 5), body_low=round(body_lo, 5),
                    wick_high=round(hi, 5), wick_low=round(lo, 5),
                    bar_index=i, mitigated=mitigated,
                    mitig_pct=round(mitig_pct, 3), is_breaker=is_breaker,
                ))

            elif direction == 'SELL' and c > o:
                # Bullish candle before bearish move → Bearish OB
                tier       = _classify_ob_tier(body, atr)
                body_lo, body_hi = o, c
                entry_depth = max(0.0, body_hi - price) / body if body else 0.0
                mitig_pct   = min(1.0, entry_depth)
                mitigated   = mitig_pct >= 0.50
                is_breaker  = price > body_hi  # price swept above OB entirely
                results.append(OrderBlock(
                    tier=tier, direction='SELL',
                    body_high=round(body_hi, 5), body_low=round(body_lo, 5),
                    wick_high=round(hi, 5), wick_low=round(lo, 5),
                    bar_index=i, mitigated=mitigated,
                    mitig_pct=round(mitig_pct, 3), is_breaker=is_breaker,
                ))
        except (IndexError, KeyError, ZeroDivisionError):
            continue

    results.sort(key=lambda x: x.bar_index, reverse=True)
    return results


# ── LIQUIDITY VOIDS ──────────────────────────────────────────

def detect_liquidity_voids(df: pd.DataFrame, atr: float,
                            lookback: int = 30) -> list:
    """
    Detect Liquidity Voids — price gaps so large they create a magnet effect.
    A void is an FVG where the gap is >= 3×ATR.
    Price will typically retrace to fill voids before continuing.
    """
    if len(df) < 3 or atr <= 0:
        return []

    n      = len(df)
    start  = max(1, n - lookback)
    voids  = []

    for i in range(start, n - 1):
        try:
            h_prev = float(df.iloc[i-1]['high'])
            l_prev = float(df.iloc[i-1]['low'])
            h_next = float(df.iloc[i+1]['high'])
            l_next = float(df.iloc[i+1]['low'])

            # Bullish void
            if l_next > h_prev:
                size = l_next - h_prev
                if size >= atr * 3.0:
                    voids.append(LiquidityVoid(
                        direction='BUY',
                        high=round(l_next, 5), low=round(h_prev, 5),
                        size=round(size, 5), bar_index=i,
                        atr_mult=round(size/atr, 2),
                    ))

            # Bearish void
            if h_next < l_prev:
                size = l_prev - h_next
                if size >= atr * 3.0:
                    voids.append(LiquidityVoid(
                        direction='SELL',
                        high=round(l_prev, 5), low=round(h_next, 5),
                        size=round(size, 5), bar_index=i,
                        atr_mult=round(size/atr, 2),
                    ))
        except (IndexError, KeyError):
            continue

    voids.sort(key=lambda x: x.bar_index, reverse=True)
    return voids


# ── MARKET STRUCTURE BREAKS ───────────────────────────────────

def detect_market_structure_breaks(df: pd.DataFrame, atr: float,
                                    lookback: int = 60) -> list:
    """
    Detect Market Structure Breaks (MSB) and Market Structure Shifts (MSS).

    MSB Bullish: price closes above a prior swing high (HH formed)
    MSB Bearish: price closes below a prior swing low (LL formed)

    MSS: the FIRST MSB after a swing reversal (indicates trend change).

    Strength classification:
    - Strong: displacement candle body >= 1.5×ATR
    - Weak: displacement candle body < 1.5×ATR
    """
    if len(df) < 10 or atr <= 0:
        return []

    n      = len(df)
    start  = max(5, n - lookback)
    breaks = []

    # Track swing highs and lows using a simple 5-bar pivot
    swings = []  # (index, price, 'high'|'low')
    for i in range(2, n-2):
        try:
            hi = float(df.iloc[i]['high'])
            lo = float(df.iloc[i]['low'])
            if (hi > float(df.iloc[i-1]['high']) and
                    hi > float(df.iloc[i-2]['high']) and
                    hi > float(df.iloc[i+1]['high']) and
                    hi > float(df.iloc[i+2]['high'])):
                swings.append((i, hi, 'high'))
            if (lo < float(df.iloc[i-1]['low']) and
                    lo < float(df.iloc[i-2]['low']) and
                    lo < float(df.iloc[i+1]['low']) and
                    lo < float(df.iloc[i+2]['low'])):
                swings.append((i, lo, 'low'))
        except (IndexError, KeyError):
            continue

    if not swings:
        return []

    # Find MSBs: candle that closes through a prior swing
    prior_msb_dir = None
    for i in range(start, n):
        try:
            c = float(df.iloc[i]['close'])
            o = float(df.iloc[i]['open'])
            body = abs(c - o)
            strength = 'strong' if body >= atr * 1.5 else 'weak'

            for sw_idx, sw_price, sw_type in swings:
                if sw_idx >= i:
                    continue

                if sw_type == 'high' and c > sw_price:
                    # Bullish MSB
                    is_mss = (prior_msb_dir == 'BEA')
                    breaks.append(MarketStructureBreak(
                        direction='BUL', strength=strength,
                        is_mss=is_mss, price=round(sw_price, 5),
                        bar_index=i, atr_at_break=round(atr, 5),
                    ))
                    prior_msb_dir = 'BUL'
                    break

                elif sw_type == 'low' and c < sw_price:
                    # Bearish MSB
                    is_mss = (prior_msb_dir == 'BUL')
                    breaks.append(MarketStructureBreak(
                        direction='BEA', strength=strength,
                        is_mss=is_mss, price=round(sw_price, 5),
                        bar_index=i, atr_at_break=round(atr, 5),
                    ))
                    prior_msb_dir = 'BEA'
                    break
        except (IndexError, KeyError):
            continue

    breaks.sort(key=lambda x: x.bar_index, reverse=True)
    return breaks[:10]  # last 10 MSBs


# ── MULTI-TF STACK SCORER ─────────────────────────────────────

def score_fvg_stack(fvg_m1: list, fvg_m15: list,
                    fvg_h4: list, price: float,
                    tolerance_pct: float = 0.002) -> float:
    """
    Score the degree to which FVGs on different timeframes stack at the same
    price level. A stack means institutional confluence — strong entry signal.

    tolerance_pct: how close FVG midpoints must be (0.2% default)
    Returns 0.0–0.30 bonus score.
    """
    if not fvg_m15:
        return 0.0

    score = 0.0
    tol   = price * tolerance_pct

    for m15_fvg in fvg_m15[:3]:   # only check 3 most recent M15 FVGs
        if m15_fvg.filled:
            continue
        mid_15 = m15_fvg.mid

        # Check M1 alignment
        m1_aligned = any(
            abs(f.mid - mid_15) <= tol
            for f in fvg_m1[:5] if not f.filled
        )
        # Check H4 alignment
        h4_aligned = any(
            abs(f.mid - mid_15) <= tol
            for f in fvg_h4[:3] if not f.filled
        )

        if m1_aligned and h4_aligned:
            score += 0.30   # triple-stack — maximum confluence
            break
        elif m1_aligned or h4_aligned:
            score += 0.12   # double-stack

    return min(0.30, score)


# ── MAIN ANALYSIS FUNCTION ────────────────────────────────────

def analyse_smc_layers(
    df_m1:  pd.DataFrame,
    df_m15: pd.DataFrame,
    df_h4:  pd.DataFrame,
    symbol: str,
    direction: str,
    atr_m15: float,
) -> SMCLayers:
    """
    Full multi-timeframe SMC analysis.
    Called from analyze_market_structure() when df_m1 and df_h4 are available.

    Returns SMCLayers with all detected structures and confluence scores.
    This runs in O(n×lookback) — fast enough for a 1-minute cycle.
    """
    layers = SMCLayers()
    price = float(df_m15.iloc[-1]['close']) if not df_m15.empty else 0.0

    # ── FVG detection per timeframe ──────────────────────────
    atr_m1 = atr_m15 * 0.25    # M1 ATR ≈ 25% of M15 ATR (rough)
    atr_h4 = atr_m15 * 4.0     # H4 ATR ≈ 4× M15 ATR

    if df_m1 is not None and len(df_m1) >= 3:
        all_m1 = detect_fvgs_tiered(df_m1, atr_m1, direction, lookback=60)
        layers.fvg_nano  = [f for f in all_m1 if f.tier == 'nano']
        layers.fvg_micro = [f for f in all_m1 if f.tier == 'micro']
        layers.fvg_ifvg  = [f for f in all_m1 if f.is_ifvg]

    if len(df_m15) >= 3:
        all_m15 = detect_fvgs_tiered(df_m15, atr_m15, direction, lookback=50)
        layers.fvg_normal = [f for f in all_m15 if not f.is_ifvg]
        layers.fvg_ifvg  += [f for f in all_m15 if f.is_ifvg]

    if df_h4 is not None and len(df_h4) >= 3:
        all_h4 = detect_fvgs_tiered(df_h4, atr_h4, direction, lookback=30)
        layers.fvg_macro = [f for f in all_h4 if not f.is_ifvg]
        layers.fvg_ifvg += [f for f in all_h4 if f.is_ifvg]

    # ── OB detection per timeframe ────────────────────────────
    if df_m1 is not None and len(df_m1) >= 4:
        all_obs_m1 = detect_obs_tiered(df_m1, atr_m1, direction, lookback=40)
        layers.ob_nano  = [o for o in all_obs_m1 if o.tier == 'nano' and not o.is_breaker]
        layers.ob_breaker = [o for o in all_obs_m1 if o.is_breaker]

    if len(df_m15) >= 4:
        all_obs_m15 = detect_obs_tiered(df_m15, atr_m15, direction, lookback=40)
        layers.ob_normal = [o for o in all_obs_m15 if not o.is_breaker]
        layers.ob_breaker += [o for o in all_obs_m15 if o.is_breaker]

    if df_h4 is not None and len(df_h4) >= 4:
        all_obs_h4 = detect_obs_tiered(df_h4, atr_h4, direction, lookback=20)
        layers.ob_macro = [o for o in all_obs_h4 if not o.is_breaker]
        layers.ob_breaker += [o for o in all_obs_h4 if o.is_breaker]

    # ── Liquidity Voids ───────────────────────────────────────
    layers.liq_voids = detect_liquidity_voids(df_m15, atr_m15, lookback=40)
    if df_h4 is not None:
        layers.liq_voids += detect_liquidity_voids(df_h4, atr_h4, lookback=20)

    # ── Market Structure Breaks ───────────────────────────────
    layers.msb_list = detect_market_structure_breaks(df_m15, atr_m15, lookback=80)
    if layers.msb_list:
        layers.last_msb = layers.msb_list[0]

    # ── Nearest structures to current price ──────────────────
    all_fvgs = (layers.fvg_nano + layers.fvg_micro +
                layers.fvg_normal + layers.fvg_macro)
    active_fvgs = [f for f in all_fvgs if not f.filled]
    if active_fvgs and price > 0:
        layers.nearest_fvg = min(active_fvgs,
                                  key=lambda f: abs(f.mid - price))

    all_obs = layers.ob_nano + layers.ob_normal + layers.ob_macro
    active_obs = [o for o in all_obs if not o.mitigated]
    if active_obs and price > 0:
        layers.nearest_ob = min(active_obs,
                                 key=lambda o: abs(o.body_low - price)
                                 if direction == 'BUY'
                                 else abs(o.body_high - price))

    if layers.ob_breaker and price > 0:
        layers.active_breaker = min(layers.ob_breaker,
                                     key=lambda o: abs(o.body_low - price))

    if layers.liq_voids and price > 0:
        layers.nearest_void = min(layers.liq_voids,
                                   key=lambda v: abs(v.low - price))

    # ── Multi-TF FVG stack score ──────────────────────────────
    layers.stack_score = score_fvg_stack(
        fvg_m1=layers.fvg_nano + layers.fvg_micro,
        fvg_m15=layers.fvg_normal,
        fvg_h4=layers.fvg_macro,
        price=price,
    )

    # ── Structure quality score ───────────────────────────────
    q = 0.0
    if layers.fvg_normal:   q += 0.15
    if layers.fvg_macro:    q += 0.10
    if layers.ob_normal:    q += 0.15
    if layers.ob_macro:     q += 0.10
    if layers.ob_breaker:   q += 0.12
    if layers.fvg_ifvg:     q += 0.08
    if layers.liq_voids:    q += 0.08
    if layers.last_msb:     q += 0.10
    if layers.stack_score > 0: q += layers.stack_score
    layers.structure_score = round(min(1.0, q), 3)

    return layers


# ── CONFLUENCE BONUS SCORER ───────────────────────────────────

def smc_confluence_bonus(layers: SMCLayers, direction: str,
                          current_score: float) -> tuple:
    """
    Compute a confidence bonus from SMC layer analysis.
    Returns (bonus: float, reasons: list[str])
    where bonus is additive to the ICT confluence score.
    Max total bonus: +0.18
    """
    bonus   = 0.0
    reasons = []

    if layers is None:
        return 0.0, []

    # Multi-TF FVG stack — strongest signal
    if layers.stack_score >= 0.25:
        bonus   += 0.12
        reasons.append("MTF_FVG_STACK_3×")
    elif layers.stack_score >= 0.10:
        bonus   += 0.06
        reasons.append("MTF_FVG_STACK_2×")

    # Inverse FVG aligned with direction
    if layers.fvg_ifvg:
        ifvg_dir = [f for f in layers.fvg_ifvg if f.direction == direction]
        if ifvg_dir:
            bonus   += 0.04
            reasons.append("IFVG_ALIGNED")

    # Breaker Block acting as support/resistance
    if layers.active_breaker:
        bonus   += 0.06
        reasons.append("BREAKER_BLOCK")

    # Liquidity Void as structural target
    if layers.nearest_void:
        bonus   += 0.04
        reasons.append(f"LIQ_VOID@{layers.nearest_void.low:.3f}")

    # Market Structure Shift (strongest trend change confirmation)
    if layers.last_msb and layers.last_msb.is_mss:
        mss_aligned = (direction == 'BUY' and layers.last_msb.direction == 'BUL') or \
                      (direction == 'SELL' and layers.last_msb.direction == 'BEA')
        if mss_aligned:
            bonus   += 0.06
            reasons.append("MSS_ALIGNED")

    # Macro OB as structural backing
    if layers.ob_macro:
        bonus   += 0.03
        reasons.append("MACRO_OB")

    return round(min(0.18, bonus), 4), reasons


# ── SUMMARY FOR LOGGING ───────────────────────────────────────

def smc_layers_summary(layers: SMCLayers) -> dict:
    """Compact dict for storing in ict_conditions / DB."""
    if layers is None:
        return {}
    return {
        'smc_fvg_nano':    len(layers.fvg_nano),
        'smc_fvg_micro':   len(layers.fvg_micro),
        'smc_fvg_normal':  len(layers.fvg_normal),
        'smc_fvg_macro':   len(layers.fvg_macro),
        'smc_ifvg':        len(layers.fvg_ifvg),
        'smc_ob_normal':   len(layers.ob_normal),
        'smc_ob_macro':    len(layers.ob_macro),
        'smc_breakers':    len(layers.ob_breaker),
        'smc_liq_voids':   len(layers.liq_voids),
        'smc_msb_count':   len(layers.msb_list),
        'smc_stack_score': layers.stack_score,
        'smc_structure_q': layers.structure_score,
        'smc_has_mss':     layers.last_msb.is_mss if layers.last_msb else False,
        'smc_nearest_fvg': layers.nearest_fvg.mid if layers.nearest_fvg else None,
        'smc_nearest_ob':  layers.nearest_ob.body_low if layers.nearest_ob else None,
        'smc_breaker_px':  layers.active_breaker.body_low if layers.active_breaker else None,
    }

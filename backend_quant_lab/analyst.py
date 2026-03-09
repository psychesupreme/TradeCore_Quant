# ============================================================
# TradeCore v52.0 — analyst.py  [SPRINT 7 ICT REBUILD]
#
# SPRINT 6 PRESERVED:
#   [OPT-1] Per-asset ATR dead-market thresholds
#   [OPT-2] EMA-20 × EMA-50 dual-confirmation macro trend
#   [OPT-3] NANO TP ratio 2.0:1
#   [OPT-4] symbol param for per-asset calibration
#
# SPRINT 7 ICT ADDITIONS:
#   [ICT-1] Order Block (OB) detection
#            Last bearish candle before bullish impulse / vice versa
#   [ICT-2] Market Structure (BOS + CHoCH)
#            Break of Structure and Change of Character
#   [ICT-3] Premium / Discount zone (50% equilibrium)
#   [ICT-4] ICT Kill Zone session weighting
#   [ICT-5] Optimal Trade Entry (OTE) Fibonacci 61.8–79%
#   [ICT-6] Equal Highs/Lows (engineered liquidity)
#   [ICT-7] Additive ICT Confluence Score
#            Replaces hardcoded base_conf = 0.85 with merit-based
#            weighted scoring. Each condition must be EARNED.
#
# SCORE ARCHITECTURE:
#   Liquidity Sweep     +0.20  (non-negotiable gate — both directions)
#   Displacement        +0.15  (non-negotiable gate)
#   ICT Kill Zone       +0.15  (London=full, NY=0.9x, other=0.5x)
#   Order Block hit     +0.15  (price returned to last OB zone)
#   FVG present         +0.10  (unfilled gap)
#   Premium/Discount    +0.10  (correct side of equilibrium)
#   BOS confirmed H4    +0.08  (structure aligned)
#   OTE zone entry      +0.07  (optimal Fibonacci retracement)
#   Volume surge ×1.10        (multiplier on final score, cap 0.99)
#
#   Max raw score = 1.00  →  after vol × = 1.00 (capped)
#   Execution threshold = 0.88 standard / 0.92 sniper
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


# ── ICT-1: ORDER BLOCK DETECTION ─────────────────────────────────────────────

def detect_order_blocks(df: pd.DataFrame, lookback: int = 20) -> dict:
    """
    An Order Block is the LAST opposite-directional candle immediately
    before a strong impulse move.

    Bullish OB: last bearish candle (close < open) before a sweep of lows
                followed by a bullish displacement candle.
    Bearish OB: last bullish candle (close > open) before a sweep of highs
                followed by a bearish displacement candle.

    OB zone is the full candle body [open, close] of the OB candle.
    Entry: price must RETURN to (retrace into) the OB zone.

    Returns:
      {'bullish': {high, low, bar_idx, active}, 'bearish': {...}}
    """
    result = {'bullish': None, 'bearish': None}
    if len(df) < 10:
        return result

    atr = df['atr'].iloc[-1] if 'atr' in df.columns else 0.001
    window = df.iloc[-lookback:]

    # ── Bullish OB: look back from current candle for last bearish candle
    #    followed by an impulse candle (close > open, body > 0.5 ATR)
    for i in range(len(window) - 2, 1, -1):
        c = window.iloc[i]
        next_c = window.iloc[i + 1] if i + 1 < len(window) else None
        if c['close'] < c['open']:                              # bearish candle
            if next_c is not None:
                body = abs(next_c['close'] - next_c['open'])
                if next_c['close'] > next_c['open'] and body > atr * 0.5:  # bullish impulse
                    ob_high = max(c['open'], c['close'])
                    ob_low  = min(c['open'], c['close'])
                    cur_price = df.iloc[-1]['close']
                    active = ob_low <= cur_price <= ob_high    # price inside OB
                    result['bullish'] = {
                        'high':     ob_high,
                        'low':      ob_low,
                        'bar_idx':  i,
                        'active':   active,
                        'retested': cur_price <= ob_high,      # price has returned
                    }
                    break

    # ── Bearish OB: last bullish candle before bearish impulse
    for i in range(len(window) - 2, 1, -1):
        c = window.iloc[i]
        next_c = window.iloc[i + 1] if i + 1 < len(window) else None
        if c['close'] > c['open']:                              # bullish candle
            if next_c is not None:
                body = abs(next_c['close'] - next_c['open'])
                if next_c['close'] < next_c['open'] and body > atr * 0.5:  # bearish impulse
                    ob_high = max(c['open'], c['close'])
                    ob_low  = min(c['open'], c['close'])
                    cur_price = df.iloc[-1]['close']
                    active = ob_low <= cur_price <= ob_high
                    result['bearish'] = {
                        'high':     ob_high,
                        'low':      ob_low,
                        'bar_idx':  i,
                        'active':   active,
                        'retested': cur_price >= ob_low,
                    }
                    break

    return result


# ── ICT-2: MARKET STRUCTURE (BOS + CHoCH) ────────────────────────────────────

def detect_market_structure(df: pd.DataFrame, swing_lookback: int = 10) -> dict:
    """
    Break of Structure (BOS): new high above the last confirmed swing high
    Change of Character (CHoCH): FIRST opposite swing against prevailing trend

    Swing High: local max over swing_lookback bars on each side
    Swing Low:  local min over swing_lookback bars on each side

    Returns:
      trend: 'BULLISH' | 'BEARISH' | 'RANGING'
      choch: True if CHoCH detected on last N bars
      last_bos_price: price of the most recent BOS
    """
    if len(df) < swing_lookback * 3:
        return {'trend': 'RANGING', 'choch': False, 'last_bos_price': None}

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
        return {'trend': 'RANGING', 'choch': False, 'last_bos_price': None}

    # Higher highs + higher lows = BULLISH structure
    last_sh = swing_highs[-1][1]
    prev_sh = swing_highs[-2][1]
    last_sl = swing_lows[-1][1]
    prev_sl = swing_lows[-2][1]

    bos_bullish = last_sh > prev_sh and last_sl > prev_sl
    bos_bearish = last_sh < prev_sh and last_sl < prev_sl

    trend = 'BULLISH' if bos_bullish else 'BEARISH' if bos_bearish else 'RANGING'

    # CHoCH: first lower high in bullish sequence, or first higher low in bearish
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
        'last_sh':        last_sh,
        'last_sl':        last_sl,
    }


# ── ICT-3: PREMIUM / DISCOUNT ─────────────────────────────────────────────────

def detect_premium_discount(df: pd.DataFrame, lookback: int = 50) -> dict:
    """
    Identifies whether current price is in a PREMIUM (above 50% of swing)
    or DISCOUNT (below 50%) zone.

    Only buy setups valid in DISCOUNT. Only sell setups in PREMIUM.
    This filters out counter-trend entries at unfavourable prices.

    equilibrium = (swing_high + swing_low) / 2
    """
    if len(df) < 10:
        return {'in_discount': False, 'in_premium': False,
                'equilibrium': None, 'pct_of_range': None}

    window = df.tail(lookback)
    swing_high = window['high'].max()
    swing_low  = window['low'].min()
    equil      = (swing_high + swing_low) / 2
    current    = df.iloc[-1]['close']

    rng         = swing_high - swing_low
    pct_of_range = ((current - swing_low) / rng) if rng > 0 else 0.5

    return {
        'in_discount':   pct_of_range < 0.50,
        'in_premium':    pct_of_range > 0.50,
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
    Asian Range:    00:00–03:00 UTC  → weight 0.60  (accumulation)
    NY Lunch (real):16:00–17:30 UTC  → weight 0.00  (EDT 12:00–13:30 — avoid)
    Other:          all else         → weight 0.50

    Returns (weight, zone_name)
    """
    t = utc_hour * 60 + utc_minute

    # [SPRINT 8 TZ-FIX] Corrected from broker-local to true UTC.
    # Previously ny_lunch was (12*60, 13*60+30) — that is London/NY Overlap,
    # the highest-volume window of the day. Real NY Lunch = EDT 12:00–13:30
    # = 16:00–17:30 UTC (UTC-4 during EDT / March–November).
    london_open  = (7*60,   9*60)       # 07:00–09:00 UTC
    london_pm    = (9*60,  12*60)       # 09:00–12:00 UTC
    london_ny    = (12*60, 16*60)       # 12:00–16:00 UTC  ← was being skipped
    ny_lunch     = (16*60, 17*60+30)    # 16:00–17:30 UTC  ← real thin window
    asian_range  = (0,      3*60)       # 00:00–03:00 UTC

    if london_open[0] <= t < london_open[1]:
        return 1.00, 'London'
    if london_ny[0] <= t < london_ny[1]:
        return 0.90, 'London_NY'
    if london_pm[0] <= t < london_pm[1]:
        return 0.70, 'London_PM'
    if asian_range[0] <= t < asian_range[1]:
        return 0.60, 'Asian'
    if ny_lunch[0] <= t < ny_lunch[1]:
        return 0.00, 'NY_Lunch'
    return 0.50, 'Other'


# ── ICT-5: OTE ZONE ──────────────────────────────────────────────────────────

def get_ote_zone(impulse_high: float, impulse_low: float,
                 direction: str) -> tuple:
    """
    Optimal Trade Entry: 61.8%–78.6% Fibonacci retracement of impulse leg.
    Price retracing INTO the OTE zone is a high-probability entry trigger.

    Bullish OTE: price pulls back to 61.8–78.6% of the up-move
    Bearish OTE: price rallies back to 61.8–78.6% of the down-move

    Returns (ote_low, ote_high) — the OTE entry zone.
    """
    rng = impulse_high - impulse_low
    if direction == 'BUY':
        ote_low  = impulse_high - rng * 0.786
        ote_high = impulse_high - rng * 0.618
    else:
        ote_low  = impulse_low  + rng * 0.618
        ote_high = impulse_low  + rng * 0.786
    return round(ote_low, 5), round(ote_high, 5)


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


# ── CORE ICT CONFLUENCE SCORER ────────────────────────────────────────────────

def compute_ict_confluence(df: pd.DataFrame, df_macro: pd.DataFrame,
                            symbol: str, market_regime: str,
                            utc_now: datetime = None) -> tuple:
    """
    Replaces detect_institutional_footprint() with a merit-based ICT
    confluence scoring system.

    Returns: (signal, score, reason_str, conditions_dict, kill_zone)

    Conditions dict is logged to the signals table for QML training.
    Every condition is independently computed — the score reflects
    exactly which factors were present, not a hardcoded floor.
    """
    if utc_now is None:
        utc_now = datetime.utcnow()

    if len(df) < 50:
        return "NEUTRAL", 0.0, "Gathering Data", {}, "N/A"

    # ── Pre-compute indicators ──────────────────────────────────────
    df = df.copy()
    df['atr']           = calculate_atr(df)
    df['liquidity_low']  = df['low'].rolling(15).min().shift(1)
    df['liquidity_high'] = df['high'].rolling(15).max().shift(1)
    df['avg_volume']     = df['volume'].rolling(15).mean()

    c1 = df.iloc[-3]
    c2 = df.iloc[-2]
    c3 = df.iloc[-1]

    current_atr = df.iloc[-1]['atr']
    avg_vol_c2  = df.iloc[-2]['avg_volume']
    vol_ratio   = c2['volume'] / avg_vol_c2 if avg_vol_c2 > 0 else 1.0

    # ── Regime multipliers ─────────────────────────────────────────
    if "DEAD MARKET" in market_regime:
        atr_mult     = 0.3
        enforce_macro = False
    elif "HIGH VOLATILITY" in market_regime:
        atr_mult     = 0.8
        enforce_macro = True
    else:
        atr_mult     = 0.5
        enforce_macro = False

    # ── Session weight ─────────────────────────────────────────────
    session_wt, kill_zone = get_ict_session_weight(utc_now.hour, utc_now.minute)
    if kill_zone == 'NY_Lunch':
        return "NEUTRAL", 0.0, "NY Lunch: Low-quality session. Skipped.", {}, kill_zone

    # ── Market structure ───────────────────────────────────────────
    structure  = detect_market_structure(df)
    h4_trend   = _derive_macro_trend(df_macro)
    macro_trend = h4_trend

    # ── Order blocks ───────────────────────────────────────────────
    obs        = detect_order_blocks(df)

    # ── Premium/Discount ───────────────────────────────────────────
    pd_zone    = detect_premium_discount(df)

    # ── Equal Highs/Lows ───────────────────────────────────────────
    eq_levels  = detect_equal_highs_lows(df)

    # ── Sweep detection ────────────────────────────────────────────
    sweep_low  = c1['low']  < c1['liquidity_low']
    sweep_high = c1['high'] > c1['liquidity_high']

    # ── Displacement ───────────────────────────────────────────────
    body_bull  = abs(c2['close'] - c2['open'])
    body_bear  = abs(c2['open']  - c2['close'])
    disp_up    = (c2['close'] > c2['open']) and (body_bull > current_atr * atr_mult)
    disp_down  = (c2['close'] < c2['open']) and (body_bear > current_atr * atr_mult)

    # ── FVG ────────────────────────────────────────────────────────
    fvg_bull   = c3['low']  > c1['high']
    fvg_bear   = c3['high'] < c1['low']

    # ── OTE zone ───────────────────────────────────────────────────
    # Use last 20-bar swing high/low as the impulse leg
    recent = df.tail(20)
    imp_high = recent['high'].max()
    imp_low  = recent['low'].min()

    # ── BULLISH SCORING ────────────────────────────────────────────
    if sweep_low and disp_up:
        score = 0.0
        cond  = {}

        cond['sweep']       = True;   score += 0.20
        cond['displacement'] = True;  score += 0.15

        # Kill zone
        zone_contrib = 0.15 * session_wt
        cond['kill_zone'] = kill_zone
        score += zone_contrib

        # Order Block
        bull_ob = obs.get('bullish')
        ob_hit  = bool(bull_ob and bull_ob.get('retested'))
        cond['order_block'] = ob_hit
        if ob_hit: score += 0.15

        # FVG
        cond['fvg'] = fvg_bull
        if fvg_bull: score += 0.10

        # Discount zone (buys should be in discount)
        in_discount = pd_zone.get('in_discount', False)
        cond['discount_zone'] = in_discount
        if in_discount: score += 0.10

        # H4 BOS aligned bullish
        bos_aligned = structure['trend'] == 'BULLISH'
        cond['bos_aligned'] = bos_aligned
        if bos_aligned: score += 0.08

        # OTE zone
        ote_low, ote_high = get_ote_zone(imp_high, imp_low, 'BUY')
        ote_hit = ote_low <= c3['close'] <= ote_high
        cond['ote'] = ote_hit
        if ote_hit: score += 0.07

        # Volume surge multiplier
        vol_surge = vol_ratio > 1.2
        cond['volume_surge'] = vol_surge
        if vol_surge:
            score = min(0.99, score * 1.10)

        # Macro filter — only block in HIGH VOL with strong bearish H4
        if enforce_macro and macro_trend == "BEARISH":
            reason = f"BUY blocked by Bearish H4 in HIGH VOL regime (score={score:.2f})"
            return "NEUTRAL", 0.0, reason, cond, kill_zone

        # Determine signal type from regime
        if "DEAD MARKET" in market_regime:
            signal = "BUY_NANO"
        elif "HIGH VOLATILITY" in market_regime:
            signal = "BUY"
        else:
            signal = "BUY_MICRO"

        reason = (f"ICT Bullish [{kill_zone}] | Score:{score:.2f} | "
                  f"OB:{ob_hit} FVG:{fvg_bull} Disc:{in_discount} "
                  f"BOS:{bos_aligned} OTE:{ote_hit} Vol:{vol_ratio:.1f}x")
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

        cond['fvg'] = fvg_bear
        if fvg_bear: score += 0.10

        in_premium = pd_zone.get('in_premium', False)
        cond['premium_zone'] = in_premium
        if in_premium: score += 0.10

        bos_aligned = structure['trend'] == 'BEARISH'
        cond['bos_aligned'] = bos_aligned
        if bos_aligned: score += 0.08

        ote_low, ote_high = get_ote_zone(imp_high, imp_low, 'SELL')
        ote_hit = ote_low <= c3['close'] <= ote_high
        cond['ote'] = ote_hit
        if ote_hit: score += 0.07

        vol_surge = vol_ratio > 1.2
        cond['volume_surge'] = vol_surge
        if vol_surge:
            score = min(0.99, score * 1.10)

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
                  f"OB:{ob_hit} FVG:{fvg_bear} Prem:{in_premium} "
                  f"BOS:{bos_aligned} OTE:{ote_hit} Vol:{vol_ratio:.1f}x")
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

# ============================================================
# TradeCore v51.0 — analyst.py
# SPRINT 4 FIXES:
#   [BUG-20] avg_vol_c2 used df.iloc[-3] (c1's row) instead of
#            df.iloc[-2] (c2's row). Volume surge check was
#            comparing c2's raw volume against c1's rolling mean,
#            causing systematic confidence miscalculation.
#   [BUG-21] detect_institutional_footprint() signature had a
#            dead 'macro_trend' default — the value was always
#            overridden by the H4-derived macro_trend in
#            analyze_market_structure(). Cleaned up to reflect
#            actual data flow. AnalysisRequest.daily_trend is
#            kept in models.py for API compatibility but is no
#            longer plumbed into signal logic.
# ============================================================

import pandas as pd
from models import AnalysisRequest, AnalysisResponse, BacktestResponse


def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high_low    = df['high'] - df['low']
    high_close  = (df['high'] - df['close'].shift()).abs()
    low_close   = (df['low']  - df['close'].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def detect_institutional_footprint(
    df: pd.DataFrame,
    macro_trend: str = "NEUTRAL",
    market_regime: str = "NORMAL",
) -> tuple[str, float, str]:
    """
    Core SMC detection engine.

    Candle reference frame:
        c1 = df.iloc[-3]  — the sweep candle
        c2 = df.iloc[-2]  — the displacement / reaction candle
        c3 = df.iloc[-1]  — the confirmation candle

    Volume baseline is always c2's own rolling average window,
    not c1's. [BUG-20 FIX]
    """
    if len(df) < 50:
        return "NEUTRAL", 0.0, "Gathering Data"

    df['liquidity_low']  = df['low'].rolling(window=15).min().shift(1)
    df['liquidity_high'] = df['high'].rolling(window=15).max().shift(1)
    df['avg_volume']     = df['volume'].rolling(window=15).mean()

    c1 = df.iloc[-3]   # sweep candle
    c2 = df.iloc[-2]   # displacement candle
    c3 = df.iloc[-1]   # confirmation candle

    current_atr = df.iloc[-1]['atr']

    # [BUG-20 FIX] Was df.iloc[-3] (c1's row) — should be df.iloc[-2] (c2's row).
    # The variable is named avg_vol_c2 and is used exclusively for comparing
    # against c2's volume. Using c1's average caused a systematic mismatch:
    # the baseline reflected a 15-bar window ending on c1 rather than c2,
    # which lagged by one candle and inflated vol_ratio on strong moves.
    avg_vol_c2 = df.iloc[-2]['avg_volume']

    # ── REGIME PARAMETERS ─────────────────────────────────────────────
    if "DEAD MARKET" in market_regime:
        vol_multiplier = 1.0
        atr_multiplier = 0.3
        regime_tag     = "[NANO]"
        signal_suffix  = "_NANO"
    elif "LOW VOLATILITY" in market_regime or "NORMAL" in market_regime:
        vol_multiplier = 1.2
        atr_multiplier = 0.5
        regime_tag     = "[MICRO]"
        signal_suffix  = ""
    else:   # HIGH VOLATILITY
        vol_multiplier = 1.5
        atr_multiplier = 0.8
        regime_tag     = "[MACRO]"
        signal_suffix  = ""

    volume_surge = c2['volume'] > (avg_vol_c2 * vol_multiplier)

    # ── DYNAMIC CONFIDENCE SCORING ────────────────────────────────────
    base_conf = 0.85
    vol_ratio = c2['volume'] / avg_vol_c2 if avg_vol_c2 > 0 else 1.0

    if   vol_ratio > 2.5: base_conf += 0.06
    elif vol_ratio > 1.8: base_conf += 0.04
    elif vol_ratio > 1.2: base_conf += 0.02

    # ── BULLISH SCENARIO ──────────────────────────────────────────────
    sweep_low        = c1['low'] < c1['liquidity_low']
    body_size_c2_bull = abs(c2['close'] - c2['open'])
    displacement_up  = (c2['close'] > c2['open']) and (body_size_c2_bull > current_atr * atr_multiplier)
    fvg_bullish      = c3['low'] > c1['high']

    if sweep_low:
        if displacement_up:
            if not volume_surge:
                return "NEUTRAL", 0.0, f"SMC {regime_tag}: Bullish FVG blocked (Low Volume Fakeout)."

            disp_ratio = body_size_c2_bull / (current_atr * atr_multiplier) if (current_atr * atr_multiplier) > 0 else 0
            if   disp_ratio > 1.5: base_conf += 0.05
            elif disp_ratio > 1.0: base_conf += 0.03

            final_conf = min(0.99, round(base_conf, 2))

            if fvg_bullish or regime_tag == "[NANO]":
                if macro_trend == "BEARISH":
                    return "NEUTRAL", 0.0, f"SMC {regime_tag}: Bullish Setup blocked by Bearish H4."
                return f"BUY{signal_suffix}", final_conf, f"SMC {regime_tag}: Bullish Setup (Vol: {vol_ratio:.1f}x)"
            return "NEUTRAL", 0.0, f"SMC {regime_tag}: Bullish Sweep + Volume. Waiting for FVG gap."
        return "NEUTRAL", 0.0, f"SMC {regime_tag}: Bullish Liquidity Swept. Waiting for volume injection."

    # ── BEARISH SCENARIO ──────────────────────────────────────────────
    sweep_high        = c1['high'] > c1['liquidity_high']
    body_size_c2_bear = abs(c2['open'] - c2['close'])
    displacement_down = (c2['close'] < c2['open']) and (body_size_c2_bear > current_atr * atr_multiplier)
    fvg_bearish       = c3['high'] < c1['low']

    if sweep_high:
        if displacement_down:
            if not volume_surge:
                return "NEUTRAL", 0.0, f"SMC {regime_tag}: Bearish FVG blocked (Low Volume Fakeout)."

            disp_ratio = body_size_c2_bear / (current_atr * atr_multiplier) if (current_atr * atr_multiplier) > 0 else 0
            if   disp_ratio > 1.5: base_conf += 0.05
            elif disp_ratio > 1.0: base_conf += 0.03

            final_conf = min(0.99, round(base_conf, 2))

            if fvg_bearish or regime_tag == "[NANO]":
                if macro_trend == "BULLISH":
                    return "NEUTRAL", 0.0, f"SMC {regime_tag}: Bearish Setup blocked by Bullish H4."
                return f"SELL{signal_suffix}", final_conf, f"SMC {regime_tag}: Bearish Setup (Vol: {vol_ratio:.1f}x)"
            return "NEUTRAL", 0.0, f"SMC {regime_tag}: Bearish Sweep + Volume. Waiting for FVG gap."
        return "NEUTRAL", 0.0, f"SMC {regime_tag}: Bearish Liquidity Swept. Waiting for volume injection."

    return "NEUTRAL", 0.0, "SMC: Price ranging inside structure. No sweeps detected."


def analyze_market_structure(
    request: AnalysisRequest,
    df_macro: pd.DataFrame = None,
    market_regime: str = "NORMAL",
) -> AnalysisResponse:
    """
    Entry point called by bot_engine.process_symbol().

    [BUG-21 FIX] request.daily_trend is intentionally NOT forwarded
    to detect_institutional_footprint(). The H4 macro_trend is derived
    directly from df_macro's EMA-20 crossover here — a live, objective
    measure. The daily_trend field on AnalysisRequest exists for API
    compatibility and future use (e.g. a weekly bias override from the
    Flutter dashboard), but driving signal logic from a hardcoded string
    passed by bot_engine ("NEUTRAL") would always block the H4 filter.
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

    # Override regime to DEAD MARKET if ATR is essentially zero
    if df.iloc[-1]['atr'] < 0.00005:
        market_regime = "DEAD MARKET"

    # Derive macro trend from H4 EMA-20 crossover
    macro_trend = "NEUTRAL"
    if df_macro is not None and not df_macro.empty and len(df_macro) > 20:
        df_macro = df_macro.copy()
        df_macro['ema_20'] = df_macro['close'].ewm(span=20, adjust=False).mean()
        current_close = df_macro.iloc[-1]['close']
        current_ema   = df_macro.iloc[-1]['ema_20']
        if current_close > current_ema:
            macro_trend = "BULLISH"
        elif current_close < current_ema:
            macro_trend = "BEARISH"

    signal, conf, reason = detect_institutional_footprint(df, macro_trend, market_regime)
    return AnalysisResponse(symbol=request.symbol, signal=signal, confidence=conf, reason=reason)


def run_backtest_strategy(request):
    return BacktestResponse(
        symbol=request.symbol,
        net_profit=0.0,
        win_rate=0.0,
        profit_factor=0.0,
        total_trades=0,
    )

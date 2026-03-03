import pandas as pd
from models import AnalysisRequest, AnalysisResponse, BacktestResponse

def calculate_atr(df, period=14):
    """Calculates volatility to measure Institutional Displacement."""
    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift()).abs()
    low_close = (df['low'] - df['close'].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def detect_institutional_footprint(df, macro_trend="NEUTRAL"):
    """
    SMC MATRIX: Scans for Liquidity Sweeps, Price Displacement, Volume Spikes, and FVGs.
    Filtered against H4 Macro Trend.
    """
    if len(df) < 50: 
        return "NEUTRAL", 0.0, "Gathering Data"

    # Map Liquidity Pools (Retail Stops)
    df['liquidity_low'] = df['low'].rolling(window=15).min().shift(1)
    df['liquidity_high'] = df['high'].rolling(window=15).max().shift(1)
    
    # Calculate Average Volume for VSA Verification
    df['avg_volume'] = df['volume'].rolling(window=15).mean()

    c1 = df.iloc[-3] 
    c2 = df.iloc[-2] 
    c3 = df.iloc[-1] 
    
    current_atr = df.iloc[-1]['atr']
    avg_vol_c2 = df.iloc[-3]['avg_volume'] # The average volume prior to the surge

    # ==========================================
    # VOLUME SPREAD ANALYSIS (VSA) CHECK
    # ==========================================
    # The displacement candle must have at least 150% of the normal resting volume
    volume_surge = c2['volume'] > (avg_vol_c2 * 1.5)

    # ==========================================
    # BULLISH SCENARIO
    # ==========================================
    sweep_low = c1['low'] < c1['liquidity_low']
    body_size_c2_bull = abs(c2['close'] - c2['open'])
    displacement_up = (c2['close'] > c2['open']) and (body_size_c2_bull > current_atr * 0.8)
    fvg_bullish = c3['low'] > c1['high']

    if sweep_low:
        if displacement_up:
            if not volume_surge:
                return "NEUTRAL", 0.0, "SMC Tracker: Bullish FVG blocked (Low Volume Fakeout / Vacuum)."
            if fvg_bullish:
                if macro_trend == "BEARISH":
                    return "NEUTRAL", 0.0, "SMC Tracker: Bullish FVG blocked by Bearish H4 Trend (Bull Trap Avoided)."
                return "BUY", 0.96, "SMC: Bullish FVG (H4 + Volume Verified)"
            return "NEUTRAL", 0.0, "SMC Tracker: Bullish Sweep + Volume Displacement. Waiting for FVG gap."
        return "NEUTRAL", 0.0, "SMC Tracker: Bullish Liquidity Swept. Waiting for volume injection."

    # ==========================================
    # BEARISH SCENARIO
    # ==========================================
    sweep_high = c1['high'] > c1['liquidity_high']
    body_size_c2_bear = abs(c2['open'] - c2['close'])
    displacement_down = (c2['close'] < c2['open']) and (body_size_c2_bear > current_atr * 0.8)
    fvg_bearish = c3['high'] < c1['low']

    if sweep_high:
        if displacement_down:
            if not volume_surge:
                return "NEUTRAL", 0.0, "SMC Tracker: Bearish FVG blocked (Low Volume Fakeout / Vacuum)."
            if fvg_bearish:
                if macro_trend == "BULLISH":
                    return "NEUTRAL", 0.0, "SMC Tracker: Bearish FVG blocked by Bullish H4 Trend (Bear Trap Avoided)."
                return "SELL", 0.96, "SMC: Bearish FVG (H4 + Volume Verified)"
            return "NEUTRAL", 0.0, "SMC Tracker: Bearish Sweep + Volume Displacement. Waiting for FVG gap."
        return "NEUTRAL", 0.0, "SMC Tracker: Bearish Liquidity Swept. Waiting for volume injection."

    return "NEUTRAL", 0.0, "SMC Tracker: Price ranging inside structure. No sweeps detected."

def analyze_market_structure(request: AnalysisRequest, df_macro=None) -> AnalysisResponse:
    df = pd.DataFrame([c.dict() for c in request.candles])
    
    if len(df) < 50: 
        return AnalysisResponse(symbol=request.symbol, signal="NEUTRAL", confidence=0.0, reason="Initializing...")

    df['atr'] = calculate_atr(df)
    
    if df.iloc[-1]['atr'] < 0.00005: 
        return AnalysisResponse(symbol=request.symbol, signal="NEUTRAL", confidence=0.0, reason="Low Volatility (Dead Market)")

    # ---------------------------------------------------------
    # MACRO TREND DETECTION (H4)
    # ---------------------------------------------------------
    macro_trend = "NEUTRAL"
    if df_macro is not None and not df_macro.empty and len(df_macro) > 20:
        # Calculate the 20 EMA on the H4 chart to find the institutional tide
        df_macro['ema_20'] = df_macro['close'].ewm(span=20, adjust=False).mean()
        current_macro_close = df_macro.iloc[-1]['close']
        current_macro_ema = df_macro.iloc[-1]['ema_20']
        
        if current_macro_close > current_macro_ema:
            macro_trend = "BULLISH"
        elif current_macro_close < current_macro_ema:
            macro_trend = "BEARISH"

    signal, conf, reason = detect_institutional_footprint(df, macro_trend)

    return AnalysisResponse(symbol=request.symbol, signal=signal, confidence=conf, reason=reason)

def run_backtest_strategy(request):
    return BacktestResponse(symbol=request.symbol, net_profit=0.0, win_rate=0.0, profit_factor=0.0, total_trades=0)
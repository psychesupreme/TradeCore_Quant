import pandas as pd
from models import AnalysisRequest, AnalysisResponse, BacktestResponse

def calculate_rsi(prices, period=14):
    delta = prices.diff()
    gain = (delta.where(delta > 0, 0)).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
    rs = gain / loss
    return (100 - (100 / (1 + rs))).fillna(50)

def calculate_atr(df, period=14):
    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift()).abs()
    low_close = (df['low'] - df['close'].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def calculate_ichimoku(df):
    h9 = df['high'].rolling(9).max(); l9 = df['low'].rolling(9).min()
    df['tenkan'] = (h9 + l9) / 2
    h26 = df['high'].rolling(26).max(); l26 = df['low'].rolling(26).min()
    df['kijun'] = (h26 + l26) / 2
    df['cloud_top'] = ((df['tenkan'] + df['kijun']) / 2).shift(26)
    return df

def check_fvg(df):
    if len(df) < 5: return "NONE"
    c1, c2, c3 = df.iloc[-4], df.iloc[-3], df.iloc[-2]
    if c2['close'] > c2['open'] and c1['high'] < c3['low']: return "BULLISH_FVG"
    if c2['close'] < c2['open'] and c1['low'] > c3['high']: return "BEARISH_FVG"
    return "NONE"

def analyze_market_structure(request: AnalysisRequest) -> AnalysisResponse:
    df = pd.DataFrame([c.dict() for c in request.candles])
    if len(df) < 50: return AnalysisResponse(symbol=request.symbol, signal="NEUTRAL", confidence=0.0, reason="No Data")

    df = calculate_ichimoku(df)
    df['rsi'] = calculate_rsi(df['close'])
    df['atr'] = calculate_atr(df)
    fvg = check_fvg(df)
    
    curr = df.iloc[-1]
    price, rsi, atr = curr['close'], curr['rsi'], curr['atr']
    
    # ATR Filter: Avoid dead markets
    if atr < 0.0001: return AnalysisResponse(symbol=request.symbol, signal="NEUTRAL", confidence=0.0, reason="Dead Market")

    signal, conf, reason = "NEUTRAL", 0.0, "Consolidation"

    if price > curr['cloud_top'] and request.daily_trend != "BEARISH":
        if curr['tenkan'] > curr['kijun']:
            signal, conf, reason = "BUY", 0.85, f"Trend: Golden Cross (RSI {int(rsi)})"
        elif fvg == "BULLISH_FVG" and rsi < 70:
            signal, conf, reason = "BUY_SMC", 0.80, "SMC: Bullish Gap"

    elif price < curr['cloud_top'] and request.daily_trend != "BULLISH":
        if curr['tenkan'] < curr['kijun']:
            signal, conf, reason = "SELL", 0.85, f"Trend: Death Cross (RSI {int(rsi)})"
        elif fvg == "BEARISH_FVG" and rsi > 30:
            signal, conf, reason = "SELL_SMC", 0.80, "SMC: Bearish Gap"

    return AnalysisResponse(symbol=request.symbol, signal=signal, confidence=conf, reason=reason)

def analyze_account_health(deals):
    if not deals: return {"total_trades": 0, "net_profit": 0, "advice": "No Data"}
    df = pd.DataFrame(deals)
    return {"total_trades": len(df), "net_profit": round(df['profit'].sum(),2), "advice": "Stable"}

def run_backtest_strategy(request):
    return BacktestResponse(symbol=request.symbol, net_profit=0.0, win_rate=0.0, profit_factor=0.0, total_trades=0)
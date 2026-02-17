from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime

class Candle(BaseModel):
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

class AnalysisRequest(BaseModel):
    symbol: str
    candles: List[Candle]
    daily_trend: str = "NEUTRAL" 

class AnalysisResponse(BaseModel):
    symbol: str
    signal: str 
    confidence: float
    reason: str

class SimulationRequest(BaseModel):
    initial_balance: float
    risk_per_trade: float
    win_rate: float
    reward_ratio: float
    total_trades: int

class SimulationResponse(BaseModel):
    final_balance: float
    max_drawdown: float
    probability_of_ruin: float
    equity_curve: List[float]

class BacktestRequest(BaseModel):
    symbol: str
    strategy: str
    initial_balance: float

class BacktestResponse(BaseModel):
    symbol: str
    net_profit: float
    win_rate: float
    profit_factor: float
    total_trades: int
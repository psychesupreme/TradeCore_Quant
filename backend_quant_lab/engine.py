import numpy as np
from models import SimulationResponse

def run_monte_carlo(request):
    initial_balance = request.initial_balance
    risk = request.risk_per_trade
    win_rate = request.win_rate
    reward = request.reward_ratio
    trades = request.total_trades
    simulations = 1000 

    outcomes = np.random.choice([1, 0], size=(simulations, trades), p=[win_rate, 1-win_rate])
    pnl_per_trade = np.where(outcomes == 1, risk * reward * initial_balance, -risk * initial_balance)
    equity_curves = initial_balance + np.cumsum(pnl_per_trade, axis=1)
    
    final_balances = equity_curves[:, -1]
    peaks = np.maximum.accumulate(equity_curves, axis=1)
    drawdowns = (peaks - equity_curves) / peaks
    max_dd = np.max(drawdowns) * 100
    ruin_count = np.sum(np.any(equity_curves <= 0, axis=1))
    prob_ruin = (ruin_count / simulations) * 100

    return SimulationResponse(
        final_balance=round(np.median(final_balances), 2),
        max_drawdown=round(max_dd, 2),
        probability_of_ruin=round(prob_ruin, 2),
        equity_curve=equity_curves[0].tolist() 
    )
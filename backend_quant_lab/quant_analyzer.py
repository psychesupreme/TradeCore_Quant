import pandas as pd
from mt5_interface import MT5Gateway

def analyze_performance():
    print("🔄 Connecting to MetaTrader 5...")
    gateway = MT5Gateway()
    if not gateway.start():
        print("❌ Failed to connect to MT5.")
        return

    print("📊 Fetching full account history from the broker...")
    deals = gateway.get_historical_deals(days=365)
    
    if not deals:
        print("⚠️ MT5 returned no history.")
        return

    df = pd.DataFrame(deals)
    if 'profit' not in df.columns or len(df) < 2:
        print("⚠️ Not enough closed trades in MT5.")
        return

    # Helper function to calculate metrics
    def calc_metrics(data, label):
        if data.empty:
            return f"{label} Metrics: No trades taken."
            
        total = len(data)
        wins = len(data[data['profit'] > 0])
        gross_profit = data[data['profit'] > 0]['profit'].sum()
        gross_loss = abs(data[data['profit'] < 0]['profit'].sum())
        net = gross_profit - gross_loss
        win_rate = (wins / total) * 100 if total > 0 else 0
        pf = (gross_profit / gross_loss) if gross_loss > 0 else float('inf')
        
        return f"{label.ljust(10)} | Trades: {str(total).ljust(4)} | Win Rate: {win_rate:5.1f}% | Net: ${net:8.2f} | PF: {pf:.2f}"

    # Macro Calculations
    total_trades = len(df)
    gross_profit = df[df['profit'] > 0]['profit'].sum()
    gross_loss = abs(df[df['profit'] < 0]['profit'].sum())
    net_profit = gross_profit - gross_loss
    win_rate = (len(df[df['profit'] > 0]) / total_trades) * 100
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float('inf')

    # Drawdown Calculation
    df['cumulative_profit'] = df['profit'].cumsum()
    df['peak'] = df['cumulative_profit'].cummax()
    df['drawdown'] = df['peak'] - df['cumulative_profit']
    max_drawdown = df['drawdown'].max()
    recovery_factor = (net_profit / max_drawdown) if max_drawdown > 0 else float('inf')

    # Asset Isolation
    df_gold = df[df['symbol'].str.contains('XAU')]
    df_fx = df[~df['symbol'].str.contains('XAU')]

    print("\n" + "="*60)
    print(" 📈 TRADECORE QUANTITATIVE ANALYSIS (ASSET ISOLATION) ")
    print("="*60)
    print(f"Total Trades Taken  : {total_trades}")
    print(f"Global Win Rate     : {win_rate:.2f}%")
    print(f"Net Realized Profit : ${net_profit:.2f}")
    print("-" * 60)
    print(f"Profit Factor       : {profit_factor:.2f}  (Target: > 1.50)")
    print(f"Max Drawdown        : ${max_drawdown:.2f}")
    print(f"Recovery Factor     : {recovery_factor:.2f}  (Target: > 3.0)")
    print("="*60)
    print(" 🥇 ASSET CLASS BREAKDOWN ")
    print("-" * 60)
    print(calc_metrics(df_gold, "GOLD (XAU)"))
    print(calc_metrics(df_fx, "FOREX"))
    print("="*60 + "\n")

if __name__ == "__main__":
    analyze_performance()
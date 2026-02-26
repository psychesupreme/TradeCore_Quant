import sqlite3
import pandas as pd
import numpy as np
import vectorbt as vbt
import warnings
import os

# Ignore standard VectorBT warnings for cleaner terminal output
warnings.filterwarnings("ignore", category=UserWarning)

def analyze_forward_test():
    # 1. Connect to the TradeCore Ledger
    try:
        conn = sqlite3.connect('tradecore_ledger.db')
        df = pd.read_sql_query("SELECT * FROM forward_test_ledger", conn)
        conn.close()
    except Exception as e:
        print(f"Error accessing database: {e}")
        return

    # 2. THE ASSET FILTER: Strictly isolate XAUUSD trades
    if not df.empty:
        df = df[df['ticker'] == 'XAUUSD'].copy()

    if df.empty or len(df) < 2:
        print("Waiting for more XAUUSD TradingView signals to generate a plot...")
        return

    # 3. Clean and format the data
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df['size'] = np.where(df['action'] == 'buy', 1.0, -1.0)
    
    # 4. Restructure for VectorBT (Flattened to 1D Series for plotting)
    close_series = df.groupby('timestamp')['price'].last()
    size_series = df.groupby('timestamp')['size'].sum()

    # 5. Initialize the VectorBT Portfolio Simulation
    pf = vbt.Portfolio.from_orders(
        close=close_series,
        size=size_series,
        size_type='amount',
        init_cash=100000,  
        fees=0.0005,
        freq='15m'
    )

    # 6. Output the Quantitative Metrics
    print("\n--- TradeCore Forward Test Results (XAUUSD ONLY) ---")
    print(pf.stats())
    
    # 7. Generate the HTML Dashboard
    print("\nGenerating interactive HTML dashboard...")
    dashboard_path = "tradecore_dashboard.html"
    
    # The plot function will now execute smoothly on the 1D data
    fig = pf.plot()
    fig.write_html(dashboard_path)
    print(f"✅ Dashboard successfully saved to: {os.path.abspath(dashboard_path)}")

if __name__ == "__main__":
    analyze_forward_test()
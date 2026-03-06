import sqlite3
import pandas as pd
import numpy as np
import vectorbt as vbt
import warnings
import os

# Ignore standard VectorBT warnings for cleaner terminal output
warnings.filterwarnings("ignore", category=UserWarning)

def analyze_forward_test():
    # [BUG-28 FIX] Was connecting to 'tradecore_ledger.db' with table
    # 'forward_test_ledger' — both are old names from an earlier prototype.
    # The live system uses 'tradecore.db' with the 'trades' table.
    # Also updated column names to match the live schema.
    try:
        conn = sqlite3.connect('tradecore.db')
        df = pd.read_sql_query(
            "SELECT * FROM trades WHERE close_time IS NOT NULL AND profit IS NOT NULL",
            conn
        )
        conn.close()
    except Exception as e:
        print(f"Error accessing database: {e}")
        return

    # Map live schema columns to the expected analysis shape
    # live trades: symbol, type, volume, open_price, open_time, close_price, close_time, profit
    if not df.empty:
        df = df[df['symbol'].str.contains('XAU', na=False)].copy()
        # Rename to match VectorBT expectations
        df = df.rename(columns={
            'open_time':   'timestamp',
            'open_price':  'price',
            'type':        'action',
        })
        df['action'] = df['action'].str.lower()  # 'BUY' → 'buy'

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
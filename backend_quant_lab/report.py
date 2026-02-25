import sqlite3
import pandas as pd
import os
from datetime import datetime

# 1. Find the Database (It might be in the current folder or one level up)
db_path = 'tradecore.db'
if not os.path.exists(db_path):
    db_path = '../tradecore.db'

print(f"--- 📊 READING DATABASE: {db_path} ---")

try:
    conn = sqlite3.connect(db_path)
    
    # 2. Calculate Profit Since Midnight
    today = datetime.now().strftime('%Y-%m-%d')
    print(f"--- SESSION REPORT ({today}) ---")
    
    query = f"SELECT symbol, count(*) as trades, sum(profit) as total_profit FROM trades WHERE close_time LIKE '{today}%' GROUP BY symbol"
    df = pd.read_sql_query(query, conn)
    
    if not df.empty:
        print(df)
        print("-" * 30)
        print(f"💰 NET PROFIT TODAY: ${df['total_profit'].sum():.2f}")
    else:
        print("No closed trades yet today.")

    # 3. Check Active Trades
    print("\n--- 🔓 OPEN POSITIONS ---")
    open_trades = pd.read_sql_query("SELECT ticket, symbol, type, volume, profit, open_time FROM trades WHERE close_time IS NULL", conn)
    
    if not open_trades.empty:
        print(open_trades)
        print("-" * 30)
        print(f"🌊 FLOATING P/L: ${open_trades['profit'].sum():.2f}")
    else:
        print("No active trades.")

    conn.close()

except Exception as e:
    print(f"❌ Error: {e}")
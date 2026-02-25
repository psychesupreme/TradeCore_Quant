import sqlite3
import pandas as pd
import MetaTrader5 as mt5
from datetime import datetime

# Connect to MT5
if not mt5.initialize():
    print("❌ MT5 Init Failed")
    quit()

# Connect to DB
conn = sqlite3.connect('tradecore.db')
cursor = conn.cursor()

print("--- 🔄 STARTING DATABASE SYNC ---")

# 1. Get all trades the DB *thinks* are open
df = pd.read_sql_query("SELECT ticket, symbol FROM trades WHERE close_time IS NULL", conn)
print(f"👻 DB believes {len(df)} trades are open. Verifying with MT5...")

closed_count = 0
total_realized_profit = 0.0

for index, row in df.iterrows():
    ticket = int(row['ticket'])
    
    # Check history for this ticket
    history = mt5.history_deals_get(ticket=ticket)
    
    if history:
        # Trade is CLOSED in real life
        deal = history[-1] # Get the exit deal
        close_price = deal.price
        profit = deal.profit
        close_time = datetime.fromtimestamp(deal.time)
        
        # Update DB
        cursor.execute("""
            UPDATE trades 
            SET close_time = ?, close_price = ?, profit = ? 
            WHERE ticket = ?
        """, (close_time, close_price, profit, ticket))
        
        closed_count += 1
        total_realized_profit += profit
        print(f"✅ CLEANED: Ticket {ticket} ({row['symbol']}) -> Profit: ${profit:.2f}")

conn.commit()

print("-" * 30)
print(f"🧹 Sync Complete. Removed {closed_count} Ghost Trades.")
print(f"💰 Realized Profit Found: ${total_realized_profit:.2f}")

# 2. Show the TRUE Open Positions
real_positions = mt5.positions_get()
print(f"\n--- 🦅 TRUE LIVE STATUS ({len(real_positions)} Active) ---")
total_floating = 0.0
for pos in real_positions:
    profit = pos.profit
    total_floating += profit
    print(f"👉 {pos.symbol} ({pos.type}) {pos.volume} Lots | P/L: ${profit:.2f}")

print("-" * 30)
print(f"🌊 REAL FLOATING P/L: ${total_floating:.2f}")

mt5.shutdown()
conn.close()
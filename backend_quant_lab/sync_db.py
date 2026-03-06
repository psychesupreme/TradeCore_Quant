import sqlite3
import pandas as pd
import MetaTrader5 as mt5
from datetime import datetime

def sync_database():
    """Automated worker to clean ghost trades from the SQLite database."""
    if not mt5.initialize():
        return

    conn = sqlite3.connect('tradecore.db')
    cursor = conn.cursor()

    try:
        # 1. Get all trades the DB *thinks* are open
        df = pd.read_sql_query("SELECT ticket, symbol FROM trades WHERE close_time IS NULL", conn)
        
        if df.empty:
            conn.close()
            return

        closed_count = 0
        total_realized_profit = 0.0

        for index, row in df.iterrows():
            ticket = int(row['ticket'])
            
            # [BUG-22 FIX] history_deals_get(ticket=) searches DEAL tickets.
            # Our 'ticket' values are POSITION/ORDER tickets, not deal tickets.
            # history_deals_get(position=) is the correct MT5 API for
            # finding all deals that belong to a given position — this is
            # why 139 ghost trades accumulated without ever being closed.
            history = mt5.history_deals_get(position=ticket)
            
            if history:
                # Trade is CLOSED in real life. Get the exit deal
                deal = history[-1] 
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

        conn.commit()
        if closed_count > 0:
            print(f"🧹 DB Sync: Removed {closed_count} Ghost Trades. Realized: ${total_realized_profit:.2f}")

    except Exception as e:
        print(f"⚠️ Sync Error: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    print("--- 🔄 RUNNING MANUAL DATABASE SYNC ---")
    sync_database()
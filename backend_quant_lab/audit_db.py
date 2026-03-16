import sqlite3
import pandas as pd

def audit_database():
    try:
        con = sqlite3.connect("tradecore.db")
        
        print("\n==================================================")
        print(" 🔬 KOM v1.0 — DATABASE INTEGRITY & FUNNEL AUDIT")
        print("==================================================\n")

        # 1. Signal Funnel Analysis
        print("📡 SIGNAL FUNNEL (All-Time)")
        print("-" * 50)
        signals_df = pd.read_sql_query("SELECT result, COUNT(*) as count FROM signals GROUP BY result", con)
        if not signals_df.empty:
            for index, row in signals_df.iterrows():
                print(f"   {row['result']:<35} : {row['count']}")
        else:
            print("   No signals recorded yet.")

        # 2. Closed Trades Analysis (The Kelly Threshold)
        print("\n🎯 CLOSED TRADES (The Kelly Threshold)")
        print("-" * 50)
        trades_df = pd.read_sql_query("SELECT profit FROM trades WHERE profit IS NOT NULL AND profit != 0 AND comment NOT LIKE '%ghost%'", con)
        
        n_trades = len(trades_df)
        if n_trades > 0:
            wins = len(trades_df[trades_df['profit'] > 0])
            losses = len(trades_df[trades_df['profit'] < 0])
            win_rate = (wins / n_trades) * 100
            net_profit = trades_df['profit'].sum()
            
            print(f"   Total Executions : {n_trades} / 30 (Minimum for ML/Kelly)")
            print(f"   Winners          : {wins}")
            print(f"   Losers           : {losses}")
            print(f"   Win Rate         : {win_rate:.1f}%")
            print(f"   Net P&L          : ${net_profit:+.2f}")
            
            if n_trades >= 30:
                print("\n   ✅ THRESHOLD MET: System is ready for Machine Learning extraction.")
            else:
                print(f"\n   ⏳ WAITING ON VOLUME: Need {30 - n_trades} more trades to achieve statistical significance.")
        else:
            print("   No closed trades recorded yet.")
            
        print("\n==================================================\n")
        con.close()
        
    except Exception as e:
        print(f"❌ Audit Error: {e}")

if __name__ == "__main__":
    audit_database()
import matplotlib
matplotlib.use('Agg') # Forces headless memory rendering
import matplotlib.pyplot as plt # Explicitly imported to safely flush memory

import pandas as pd
import mplfinance as mpf
import os
from datetime import datetime

class VisionEngine:
    @staticmethod
    def generate_trade_snapshot(df, symbol, action, entry_price, sl, tp, confidence):
        """Generates a candlestick chart with SL/TP lines for Telegram delivery"""
        try:
            # 1. Route to the Media Folder
            media_dir = "Media" 
            os.makedirs(media_dir, exist_ok=True)

            df_plot = df.copy()
            df_plot.set_index('time', inplace=True)
            df_plot = df_plot.tail(50)

            # 2. Unique timestamp to prevent file locking
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"snapshot_{symbol}_{action}_{timestamp}.png"
            file_path = os.path.join(media_dir, filename)
            
            hline_config = dict(
                hlines=[entry_price, sl, tp],
                colors=['#2962FF', '#FF5252', '#00C853'],
                linestyle='--'
            )

            mc = mpf.make_marketcolors(up='#00C853', down='#FF5252', edge='inherit', wick='inherit')
            s  = mpf.make_mpf_style(marketcolors=mc, base_mpf_style='nightclouds')

            # Clean up the action text for the chart title
            clean_action = action.replace("_", " ")
            title = f"{symbol} {clean_action} | Conf: {confidence*100:.0f}%"

            # 3. Save to the Media path
            mpf.plot(
                df_plot, 
                type='candle', 
                style=s, 
                hlines=hline_config, 
                title=title,
                savefig=file_path
            )
            
            plt.close('all') 
            return file_path
            
        except Exception as e:
            print(f"⚠️ Vision Engine Error: {e}")
            return None
            
    @staticmethod
    def cleanup_snapshot(file_path):
        """Deletes the snapshot file after sending."""
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception as e:
                print(f"⚠️ Manual cleanup needed for {file_path}: {e}")
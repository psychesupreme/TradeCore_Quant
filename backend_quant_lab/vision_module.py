import matplotlib
matplotlib.use('Agg') # Forces headless memory rendering
import matplotlib.pyplot as plt # Explicitly imported to safely flush memory

import pandas as pd
import mplfinance as mpf
import os

class VisionEngine:
    @staticmethod
    def generate_trade_snapshot(df, symbol, action, entry_price, sl, tp, confidence):
        """Generates a candlestick chart with SL/TP lines for Telegram delivery"""
        try:
            df_plot = df.copy()
            df_plot.set_index('time', inplace=True)
            df_plot = df_plot.tail(50)

            filename = f"snapshot_{symbol}.png"
            
            hline_config = dict(
                hlines=[entry_price, sl, tp],
                colors=['#2962FF', '#FF5252', '#00C853'],
                linestyle='--'
            )

            mc = mpf.make_marketcolors(up='#00C853', down='#FF5252', edge='inherit', wick='inherit')
            s  = mpf.make_mpf_style(marketcolors=mc, base_mpf_style='nightclouds')

            title = f"{symbol} {action} | Conf: {confidence*100:.0f}%"

            # Generate and save the image
            mpf.plot(
                df_plot, 
                type='candle', 
                style=s, 
                hlines=hline_config, 
                title=title,
                savefig=filename
            )
            
            # Safely flush the RAM immediately after saving to prevent memory leaks
            plt.close('all') 
            
            return filename
        except Exception as e:
            print(f"⚠️ Vision Engine Error: {e}")
            return None
            
    @staticmethod
    def cleanup_snapshot(filename):
        if filename and os.path.exists(filename):
            try:
                os.remove(filename)
            except:
                pass
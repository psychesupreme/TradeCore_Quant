# File: backend_quant_lab/telegram_client.py

import requests

class TelegramNotifier:
    def __init__(self, token=None, chat_id=None):
        # HARDCODE YOUR KEYS HERE FOR SIMPLICITY
        # Replace these with your actual keys from Step 1
        self.token = token or "8357033749:AAH05DRZxdtvQv8l2rtOLUeBjCijXODw5Zw" 
        self.chat_id = chat_id or "5268311560"
        self.base_url = f"https://api.telegram.org/bot{self.token}/sendMessage"

    def send(self, message):
        """
        Sends a text message to your Telegram account.
        """
        if "YOUR_" in self.token:
            print("Telegram Error: Token not set.")
            return False

        try:
            payload = {
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": "Markdown" # Allows bold/italic text
            }
            response = requests.post(self.base_url, json=payload, timeout=5)
            
            if response.status_code == 200:
                return True
            else:
                print(f"Telegram Failed: {response.text}")
                return False
        except Exception as e:
            print(f"Telegram Connection Error: {e}")
            return False

    def send_trade_alert(self, symbol, action, lot_size, reason):
        """
        Formats a beautiful trade alert.
        """
        emoji = "🟢" if action == "BUY" else "🔴"
        msg = (
            f"{emoji} *TRADE EXECUTED*\n"
            f"-------------------\n"
            f"Symbol: *{symbol}*\n"
            f"Action: *{action}*\n"
            f"Size: *{lot_size} Lots*\n"
            f"Logic: _{reason}_\n"
            f"-------------------\n"
            f"🤖 TradeCore v27 Auto-Pilot"
        )
        self.send(msg)
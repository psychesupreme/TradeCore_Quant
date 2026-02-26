import requests
import threading
import time
import os

class TelegramNotifier:
    def __init__(self):
        # 1. PASTE YOUR ACTUAL CREDENTIALS HERE
        self.bot_token = "8357033749:AAH05DRZxdtvQv8l2rtOLUeBjCijXODw5Zw"
        self.chat_id = "5268311560"
        
        self.is_listening = False
        self.last_update_id = 0

    def send(self, message):
        """Sends a standard formatted text message to Telegram"""
        if not self.bot_token or self.bot_token == "PASTE_YOUR_BOT_TOKEN_HERE":
            print("⚠️ Telegram Token not set. Message not sent.")
            return None
            
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": "Markdown"
        }
        try:
            response = requests.post(url, json=payload, timeout=10)
            return response.json()
        except Exception as e:
            print(f"⚠️ Telegram Send Error: {e}")
            return None

    def send_photo(self, photo_path, caption=""):
        """Uploads a generated chart image to the Telegram chat"""
        if not self.bot_token or self.bot_token == "PASTE_YOUR_BOT_TOKEN_HERE":
            print("⚠️ Telegram Token not set. Photo not sent.")
            return None
            
        url = f"https://api.telegram.org/bot{self.bot_token}/sendPhoto"
        try:
            with open(photo_path, 'rb') as photo:
                files = {'photo': photo}
                data = {'chat_id': self.chat_id, 'caption': caption, 'parse_mode': 'Markdown'}
                response = requests.post(url, files=files, data=data, timeout=20)
                return response.json()
        except Exception as e:
            print(f"⚠️ Telegram Photo Error: {e}")
            return None

    def start_listening(self, command_handler):
        if not self.bot_token or self.bot_token == "PASTE_YOUR_BOT_TOKEN_HERE":
            return
        self.is_listening = True
        threading.Thread(target=self._poll_commands, args=(command_handler,), daemon=True).start()

    def stop_listening(self):
        self.is_listening = False

    def _poll_commands(self, command_handler):
        url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates"
        while self.is_listening:
            try:
                payload = {"offset": self.last_update_id + 1, "timeout": 5}
                response = requests.get(url, params=payload, timeout=10)
                data = response.json()
                
                if data.get("ok"):
                    for update in data.get("result", []):
                        self.last_update_id = update["update_id"]
                        message = update.get("message", {})
                        text = message.get("text", "")
                        
                        if text.startswith("/"):
                            command_handler(text)
            except:
                pass
            time.sleep(2)

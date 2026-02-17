import requests
import time
import threading
import os

class TelegramNotifier:
    def __init__(self, token=None, chat_id=None):
        # Allow hardcoding or env vars
        self.token = token or "8357033749:AAH05DRZxdtvQv8l2rtOLUeBjCijXODw5Zw" 
        self.chat_id = chat_id or "5268311560"
        self.base_url = f"https://api.telegram.org/bot{self.token}"
        self.offset = 0
        self.running = False

    def send(self, message):
        try:
            url = f"{self.base_url}/sendMessage"
            payload = {"chat_id": self.chat_id, "text": message, "parse_mode": "Markdown"}
            requests.post(url, json=payload, timeout=5)
        except Exception as e:
            print(f"⚠️ Telegram Send Error: {e}")

    def start_listening(self, command_handler):
        """Starts a background thread to poll for commands"""
        self.running = True
        thread = threading.Thread(target=self._poll_updates, args=(command_handler,))
        thread.daemon = True
        thread.start()

    def stop_listening(self):
        self.running = False

    def _poll_updates(self, handler):
        print("🎧 Telegram Command Listener Active...")
        while self.running:
            try:
                url = f"{self.base_url}/getUpdates?offset={self.offset}&timeout=30"
                resp = requests.get(url, timeout=35)
                data = resp.json()
                
                if data.get("ok"):
                    for result in data["result"]:
                        self.offset = result["update_id"] + 1
                        message = result.get("message", {})
                        text = message.get("text", "")
                        
                        if text.startswith("/"):
                            handler(text)
            except Exception:
                time.sleep(5)  # Backoff on error
            time.sleep(1)
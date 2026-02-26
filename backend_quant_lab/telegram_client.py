import telebot
import threading
import time

class TelegramNotifier:
    def __init__(self):
        # 1. EXACTLY replace the text inside the quotes with your real credentials
        self.token = "8357033749:AAH05DRZxdtvQv8l2rtOLUeBjCijXODw5Zw"
        self.chat_id = "5268311560"
        
        # 2. THIS LINE IS CRITICAL - It creates the bot. Do not delete it!
        self.bot = telebot.TeleBot(self.token)
        
        self.is_listening = False
        self.polling_thread = None

    def send(self, message):
        def _send_async():
            for attempt in range(5):
                try:
                    self.bot.send_message(
                        self.chat_id, 
                        message, 
                        parse_mode="Markdown", 
                        timeout=60
                    )
                    break 
                except Exception as e:
                    print(f"⚠️ Telegram Network Latency ({e}). Retry {attempt+1}/5...")
                    time.sleep(5)
        threading.Thread(target=_send_async).start()

    def send_photo(self, photo_path, caption=""):
        def _send_photo_async():
            for attempt in range(5):
                try:
                    with open(photo_path, 'rb') as photo:
                        self.bot.send_photo(
                            self.chat_id, 
                            photo, 
                            caption=caption, 
                            parse_mode="Markdown", 
                            timeout=60
                        )
                    break 
                except Exception as e:
                    print(f"⚠️ Telegram Image Upload Latency: {e}. Retry {attempt+1}/5...")
                    time.sleep(10)
        threading.Thread(target=_send_photo_async).start()

    def start_listening(self, command_callback):
        self.is_listening = True
        
        @self.bot.message_handler(func=lambda message: True)
        def handle_message(message):
            if str(message.chat.id) == str(self.chat_id):
                command_callback(message.text)
                
        def poll():
            while self.is_listening:
                try:
                    # NEW: Production-grade Infinity Polling for unstable networks
                    self.bot.infinity_polling(timeout=20, long_polling_timeout=15, logger_level=0)
                except Exception as e:
                    print(f"Telegram polling error: {e}")
                    time.sleep(5)
                    
        self.polling_thread = threading.Thread(target=poll, daemon=True)
        self.polling_thread.start()

    def stop_listening(self):
        self.is_listening = False
        if self.bot:
            self.bot.stop_polling()

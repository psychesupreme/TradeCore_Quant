import os
import telebot
import telebot.apihelper as apihelper
import threading
import time

class TelegramNotifier:
    def __init__(self):
        # [SECURITY FIX S35 / BUG-75] Token MUST come from environment variable.
        # The hardcoded fallback was removed — it exposed credentials in source
        # control and in this conversation. Rotate the old token in BotFather
        # immediately, then set TELEGRAM_BOT_TOKEN in your environment.
        #
        # Windows:  setx TELEGRAM_BOT_TOKEN "your_token_here"
        # Linux:    export TELEGRAM_BOT_TOKEN="your_token_here"
        # Or add to a .env file loaded at startup (never commit .env to git).
        self.token = os.getenv("TELEGRAM_BOT_TOKEN")
        if not self.token:
            raise ValueError(
                "TELEGRAM_BOT_TOKEN environment variable is not set.\n"
                "Set it before starting Kom: export TELEGRAM_BOT_TOKEN='your_token'"
            )
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "5268311560")

        # [PROXY FIX] api.telegram.org is blocked at ISP level on this machine.
        # If TELEGRAM_PROXY is set in the environment, route all Telegram API
        # calls through it. Supports SOCKS5 (e.g. from a local VPN/proxy tool)
        # and plain HTTPS proxies.
        #
        # Examples:
        #   SOCKS5 (e.g. Tor, SSH tunnel):  socks5://127.0.0.1:1080
        #   HTTPS proxy:                    http://127.0.0.1:8080
        #   No proxy (default):             leave unset — direct connection
        #
        proxy = os.getenv("TELEGRAM_PROXY", "")
        if proxy:
            apihelper.proxy = {"https": proxy}
            print(f"📡 Telegram: Routing via proxy ({proxy})")
        else:
            # No proxy configured — direct connection attempted.
            # If api.telegram.org is blocked on this network, alerts will
            # retry up to 3 times at 5s intervals then give up silently.
            print(f"📡 Telegram: Direct connection (no proxy configured)")

        self.bot = telebot.TeleBot(self.token)
        
        self.is_listening = False
        self.polling_thread = None

    def send(self, message):
        def _send_async():
            for attempt in range(3):   # [FIX] Was 5 — with 7+ concurrent signals,
                                       # 5 retries × 5s sleep = 25s+ per thread,
                                       # causing a pile-up of blocked alert threads.
                                       # 3 retries is sufficient for transient drops.
                try:
                    self.bot.send_message(
                        self.chat_id,
                        message,
                        parse_mode="Markdown",
                        timeout=8      # [FIX] Was 60 — 8s matches the original
                                       # startup message timeout and is enough for
                                       # a healthy Telegram API response. 60s blocked
                                       # each thread for a full minute before retry.
                    )
                    break
                except Exception as e:
                    print(f"⚠️ Telegram Network Latency ({e}). Retry {attempt+1}/3...")
                    time.sleep(5)
        threading.Thread(target=_send_async, daemon=True).start()

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
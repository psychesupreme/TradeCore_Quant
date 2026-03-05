# ============================================================
# TradeCore v51.0 — telegram_client.py
# SPRINT 1 FIXES APPLIED:
#   [BUG-10] Bot token and chat ID loaded from environment
#            variables — never hardcoded in source code.
#
# SETUP INSTRUCTIONS:
#   1. Revoke your old token via BotFather (/revoke command)
#      if it was ever committed to git or shared.
#   2. Create a new token via BotFather (/newbot or /token).
#   3. Set environment variables before starting the server:
#
#      Windows CMD:
#        set TELEGRAM_BOT_TOKEN=your_new_token_here
#        set TELEGRAM_CHAT_ID=your_chat_id_here
#
#      Windows PowerShell:
#        $env:TELEGRAM_BOT_TOKEN="your_new_token_here"
#        $env:TELEGRAM_CHAT_ID="your_chat_id_here"
#
#      Linux/Mac:
#        export TELEGRAM_BOT_TOKEN="your_new_token_here"
#        export TELEGRAM_CHAT_ID="your_chat_id_here"
#
#   4. To find your chat ID: message @userinfobot on Telegram.
#
# For persistent configuration on Windows, set these in:
#   System Properties → Environment Variables → User Variables
# ============================================================

import os
import telebot
import threading
import time


class TelegramNotifier:
    def __init__(self):
        # [BUG-10 FIX] Load from environment — never hardcode credentials.
        self.token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

        if not self.token:
            print("⚠️  WARNING: TELEGRAM_BOT_TOKEN environment variable not set.")
            print("   Telegram alerts will be disabled until token is configured.")
            self.bot = None
            return

        if not self.chat_id:
            print("⚠️  WARNING: TELEGRAM_CHAT_ID environment variable not set.")
            print("   Telegram alerts will be disabled until chat ID is configured.")
            self.bot = None
            return

        # Bot object is only created when both credentials are present
        self.bot = telebot.TeleBot(self.token)
        self.is_listening = False
        self.polling_thread = None
        print("✅ Telegram: Notifier initialized.")

    def _is_configured(self) -> bool:
        """Returns True only if bot is ready to send messages."""
        return self.bot is not None and bool(self.token) and bool(self.chat_id)

    def send(self, message: str):
        """Sends a Markdown-formatted message. Non-blocking (runs in thread)."""
        if not self._is_configured():
            # Silently skip — warning was shown at startup
            return

        def _send_async():
            for attempt in range(5):
                try:
                    self.bot.send_message(
                        self.chat_id,
                        message,
                        parse_mode="Markdown",
                        timeout=60
                    )
                    return  # Success
                except Exception as e:
                    print(f"⚠️  Telegram Network Latency ({e}). Retry {attempt + 1}/5...")
                    time.sleep(5)
            print("❌ Telegram: Failed to send message after 5 retries.")

        threading.Thread(target=_send_async, daemon=True).start()

    def send_photo(self, photo_path: str, caption: str = ""):
        """Sends a photo file. Used by VisionEngine for chart snapshots."""
        if not self._is_configured():
            return

        def _send_photo_async():
            try:
                with open(photo_path, 'rb') as img:
                    self.bot.send_photo(
                        self.chat_id,
                        img,
                        caption=caption,
                        parse_mode="Markdown",
                        timeout=60
                    )
            except Exception as e:
                print(f"⚠️  Telegram Photo Send Failed: {e}")

        threading.Thread(target=_send_photo_async, daemon=True).start()

    def start_listening(self, command_callback):
        """Starts the Telegram command listener in a background daemon thread."""
        if not self._is_configured():
            print("⚠️  Telegram: Listening disabled — credentials not configured.")
            return

        self.is_listening = True

        @self.bot.message_handler(func=lambda message: True)
        def handle_message(message):
            # Only respond to messages from the authorised chat ID
            if str(message.chat.id) == str(self.chat_id):
                command_callback(message.text)

        def poll():
            while self.is_listening:
                try:
                    # Production-grade infinity polling handles network drops automatically
                    self.bot.infinity_polling(
                        timeout=20,
                        long_polling_timeout=15,
                        logger_level=0
                    )
                except Exception as e:
                    print(f"⚠️  Telegram polling error: {e}. Reconnecting in 5s...")
                    time.sleep(5)

        self.polling_thread = threading.Thread(target=poll, daemon=True)
        self.polling_thread.start()
        print("✅ Telegram: Command listener active.")

    def stop_listening(self):
        """Gracefully stops the polling thread."""
        self.is_listening = False
        if self.bot:
            try:
                self.bot.stop_polling()
            except Exception:
                pass
        print("✅ Telegram: Listener stopped.")

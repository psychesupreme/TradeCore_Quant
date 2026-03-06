# ============================================================
# TradeCore v51.0 — telegram_client.py
# SPRINT 1 FIXES:  BUG-10
# HOTFIX APPLIED:
#   [HF-C] Timeout reduced 60s → 8s. Retries reduced 5 → 2.
#          Was burning 5 minutes per failed alert (300s total).
#          Now fails fast in ≤24s and moves on.
#   [HF-C] Bot token truncated in error log URLs — was printing
#          the full API token in plain text in the console.
#   [HF-D] send_photo() now signals cleanup via callback after
#          the file handle is fully closed, fixing WinError 32.
# ============================================================

import os
import telebot
import threading
import time


# How many chars of token to show in logs (just enough to identify
# which bot without exposing the full secret).
_TOKEN_SHOW_CHARS = 8


def _safe_token(token: str) -> str:
    """Returns the last N chars of the token for log messages."""
    if len(token) <= _TOKEN_SHOW_CHARS:
        return "***"
    return f"...{token[-_TOKEN_SHOW_CHARS:]}"


class TelegramNotifier:
    def __init__(self):
        self.token   = os.environ.get("TELEGRAM_BOT_TOKEN", "8357033749:AAH05DRZxdtvQv8l2rtOLUeBjCijXODw5Zw")
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "5268311560")

        if not self.token:
            print("⚠️  WARNING: TELEGRAM_BOT_TOKEN not set. Alerts disabled.")
            self.bot = None
            return

        if not self.chat_id:
            print("⚠️  WARNING: TELEGRAM_CHAT_ID not set. Alerts disabled.")
            self.bot = None
            return

        self.bot = telebot.TeleBot(self.token)
        self.is_listening = False
        self.polling_thread = None
        print(f"✅ Telegram: Notifier initialized (bot ...{self.token[-8:]}).")

    def _is_configured(self) -> bool:
        return self.bot is not None and bool(self.token) and bool(self.chat_id)

    def send(self, message: str):
        """Non-blocking Markdown message dispatch."""
        if not self._is_configured():
            return

        def _send_async():
            # [HF-C] Max 2 retries. Timeout 8s. Was 5 retries × 60s = 300s blocked.
            for attempt in range(2):
                try:
                    self.bot.send_message(
                        self.chat_id,
                        message,
                        parse_mode="Markdown",
                        timeout=8,
                    )
                    return  # success
                except Exception as e:
                    # [HF-C] Truncate token so it doesn't appear in logs
                    safe_err = str(e).replace(self.token, _safe_token(self.token))
                    print(f"⚠️  Telegram Latency ({safe_err}). Retry {attempt + 1}/2...")
                    time.sleep(3)
            print("❌ Telegram: Alert dropped after 2 retries. Will retry next event.")

        threading.Thread(target=_send_async, daemon=True).start()

    def send_photo(self, photo_path: str, caption: str = "",
                   on_complete=None):
        """
        Sends a photo file.
        [HF-D] on_complete callback is invoked AFTER the file handle
        is closed — not before. This fixes WinError 32 where
        cleanup_snapshot deleted the file while Telegram still had
        it open during an upload attempt.
        """
        if not self._is_configured():
            if on_complete:
                on_complete()
            return

        def _send_photo_async():
            try:
                with open(photo_path, "rb") as img:
                    self.bot.send_photo(
                        self.chat_id,
                        img,
                        caption=caption,
                        parse_mode="Markdown",
                        timeout=8,   # [HF-C] same fast timeout
                    )
                # File handle is guaranteed closed here (with block exited)
            except Exception as e:
                safe_err = str(e).replace(self.token, _safe_token(self.token))
                print(f"⚠️  Telegram Photo Failed: {safe_err}")
            finally:
                # Trigger cleanup AFTER file is closed regardless of success/failure
                if on_complete:
                    try:
                        on_complete()
                    except Exception as ce:
                        print(f"⚠️  Photo cleanup callback error: {ce}")

        threading.Thread(target=_send_photo_async, daemon=True).start()

    def start_listening(self, command_callback):
        """Starts the Telegram command listener in a background daemon thread."""
        if not self._is_configured():
            print("⚠️  Telegram: Listening disabled — credentials not configured.")
            return

        self.is_listening = True

        @self.bot.message_handler(func=lambda message: True)
        def handle_message(message):
            if str(message.chat.id) == str(self.chat_id):
                command_callback(message.text)

        def poll():
            while self.is_listening:
                try:
                    self.bot.infinity_polling(
                        timeout=20,
                        long_polling_timeout=15,
                        logger_level=0,
                    )
                except Exception as e:
                    safe_err = str(e).replace(self.token, _safe_token(self.token))
                    print(f"⚠️  Telegram polling error: {safe_err}. Reconnecting in 5s...")
                    time.sleep(5)

        self.polling_thread = threading.Thread(target=poll, daemon=True)
        self.polling_thread.start()
        print("✅ Telegram: Command listener active.")

    def stop_listening(self):
        self.is_listening = False
        if self.bot:
            try:
                self.bot.stop_polling()
            except Exception:
                pass
        print("✅ Telegram: Listener stopped.")

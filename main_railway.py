"""A small Telegram membership-gate bot with video-code access.

The bot logic stays in Python. Telegram requests are sent through the local
Node connector bridge so the Replit-managed Telegram connection can handle
authentication without exposing a bot token to the application.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


LOGGER = logging.getLogger("telegram_bot")
MAX_MESSAGE_LENGTH = 4096
INVITE_LINK = "https://t.me/+JImR6jNWyEBmOWM1"
REGISTRATION_URL = "https://t.me/MrsChicksOfficial"
CHECK_ACCESS_CALLBACK = "check_access"
ID_COMMAND = "/id"
CHAT_ID_COMMAND = "/chatid"
POST_PREVIEW_COMMAND = "/postpreview"
ACCESS_STATUSES = {"creator", "administrator", "member"}
WELCOME_MESSAGE = "🔒 Akses khusus member.\nSilakan join channel terlebih dahulu."
VIDEO_DENIED_MESSAGE = "❌ Akses ditolak.\nVideo full hanya untuk member."
ACCESS_GRANTED_MESSAGE = "✅ Akses diterima."
VIDEO_CODES: dict[str, dict[str, str]] = {
    "MC001": {
        "title": "Test video uploaded in the private channel",
        "preview_url": "https://t.me/MrsChicksAccessBot?start=MC001",
        "url": "https://t.me/c/4380459585/61",
    },
}


class TelegramError(RuntimeError):
    """Raised when Telegram or the connector bridge reports an error."""

class ConnectorBridge:
    def __init__(self, bot_token: str) -> None:
        self.base_url = f"https://api.telegram.org/bot{bot_token}"

    @classmethod
    def start(cls) -> "ConnectorBridge":
        bot_token = os.environ.get("BOT_TOKEN", "").strip()
        if not bot_token:
            raise TelegramError("BOT_TOKEN is not configured.")
        return cls(bot_token)

    def request(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload or {}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=35) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8", errors="replace")
            try:
                error_data = json.loads(raw)
                description = error_data.get("description", raw)
            except json.JSONDecodeError:
                description = raw
            raise TelegramError(
                f"Telegram API error: {description}"
            ) from error
        except Exception as error:
            raise TelegramError(
                f"Telegram request failed: {error}"
            ) from error

        if not isinstance(data, dict) or not data.get("ok"):
            description = (
                data.get("description", "Unknown Telegram error")
                if isinstance(data, dict)
                else "Invalid Telegram response"
            )
            raise TelegramError(str(description))

        return data.get("result")

    def close(self) -> None:
        pass
class TelegramBot:
    """Telegram membership gate with join and access-check actions."""

    def __init__(self, bridge: ConnectorBridge) -> None:
        self.bridge = bridge
        self.running = True
        self.offset = 0
        self.required_channel_id = os.environ.get("REQUIRED_CHANNEL_ID", "").strip()
        admin_user_id = os.environ.get("ADMIN_USER_ID", "").strip()
        try:
            self.admin_user_id = int(admin_user_id) if admin_user_id else None
        except ValueError:
            self.admin_user_id = None
        self.preview_chat_id = os.environ.get("PREVIEW_CHAT_ID", "").strip()
        self.pending_preview_codes: dict[str, str] = {}
        if not self.required_channel_id:
            raise TelegramError("REQUIRED_CHANNEL_ID is not configured.")

    def telegram(self, path: str, payload: dict[str, Any] | None = None) -> Any:
        return self.bridge.request(path, payload)

    @staticmethod
    def access_keyboard(
        video_code: str | None = None,
    ) -> dict[str, list[list[dict[str, str]]]]:
        callback_data = CHECK_ACCESS_CALLBACK

        if video_code is not None:
            callback_data = f"{CHECK_ACCESS_CALLBACK}:{video_code}"

        keyboard = [
            [
                {
                    "text": "📩 REGISTRASI / INFO JOIN",
                    "url": REGISTRATION_URL,
                }
            ],
            [
                {
                    "text": "✅ CHECK ACCESS",
                    "callback_data": callback_data,
                }
            ],
        ]

        return {"inline_keyboard": keyboard}
            "inline_keyboard": [
                [
                    {
                        "text": "🎬 BUKA VIDEO FULL",
                        "url": VIDEO_CODES[video_code]["url"],
                    }
                ]
            ]
        }

    @staticmethod
    def preview_keyboard(video_code: str) -> dict[str, list[list[dict[str, str]]]]:
        return {
            "inline_keyboard": [
                [
                    {
                        "text": f"🔓 AKSES {video_code}",
                        "url": VIDEO_CODES[video_code]["preview_url"],
                    }
                ]
            ]
        }

    @staticmethod
    def video_code_from_start(text: str) -> str | None:
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            return None
        candidate = parts[1].split(maxsplit=1)[0].strip().upper()
        return candidate if candidate in VIDEO_CODES else None

    def has_channel_access(self, user_id: int) -> bool:
        member = self.telegram(
            "/getChatMember",
            {"chat_id": self.required_channel_id, "user_id": user_id},
        )
        status = member.get("status") if isinstance(member, dict) else None
        return status in ACCESS_STATUSES

    @staticmethod
    def video_access_message(video_code: str) -> str:
        return ACCESS_GRANTED_MESSAGE

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        reply_to: int | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text[:MAX_MESSAGE_LENGTH],
        }
        if reply_to is not None:
            payload["reply_parameters"] = {"message_id": reply_to}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            self.telegram("/sendMessage", payload)
        except TelegramError as error:
            if reply_to is None or not self.is_reply_target_error(error):
                raise
            LOGGER.warning(
                "Telegram rejected reply target; retrying message without reply_parameters."
            )
            payload.pop("reply_parameters", None)
            self.telegram("/sendMessage", payload)

    @staticmethod
    def is_reply_target_error(error: TelegramError) -> bool:
        details = str(error).lower()
        return (
            "reply_parameters" in details
            or "message to be replied not found" in details
            or "reply message not found" in details
        )

    @staticmethod
    def is_message_not_modified_error(error: TelegramError) -> bool:
        return "message is not modified" in str(error).lower()

    def send_video(
        self,
        chat_id: int | str,
        file_id: str,
        caption: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "video": file_id,
            "caption": caption[:1024],
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        self.telegram("/sendVideo", payload)

    def edit_message(
        self,
        chat_id: int | str,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text[:MAX_MESSAGE_LENGTH],
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        self.telegram("/editMessageText", payload)

    def answer_callback(
        self,
        callback_query_id: str,
        text: str | None = None,
        show_alert: bool = False,
    ) -> Any:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text is not None:
            payload["text"] = text[:200]
        if show_alert:
            payload["show_alert"] = True
        self.telegram("/answerCallbackQuery", payload)

    def handle_message(self, message: dict[str, Any]) -> None:
        chat = message.get("chat", {})
        chat_id = chat.get("id")
        message_id = message.get("message_id")
        user = message.get("from", {})
        user_id = user.get("id") if isinstance(user, dict) else None
        text = message.get("text")

        pending_code = (
            self.pending_preview_codes.get(str(chat_id))
            if chat_id is not None
            else None
        )
        if pending_code is not None and self.admin_user_id == user_id:
            video = message.get("video")
            if isinstance(video, dict):
                file_id = video.get("file_id")
                if not isinstance(file_id, str) or not file_id:
                    self.send_message(
                        chat_id,
                        "❌ Video preview tidak memiliki file yang dapat dipublikasikan.",
                    )
                    return
                try:
                    self.send_video(
                        self.preview_chat_id,
                        file_id,
                        f"🎬 VIDEO PREVIEW\n\n🔐 CODE: {pending_code}",
                        self.preview_keyboard(pending_code),
                    )
                except TelegramError:
                    LOGGER.exception("Preview publishing failed for %s.", pending_code)
                    self.send_message(
                        chat_id,
                        "❌ Gagal mempublikasikan video preview. Silakan kirim ulang.",
                    )
                    return
                self.pending_preview_codes.pop(str(chat_id), None)
                self.send_message(chat_id, f"✅ Preview {pending_code} berhasil dipublikasikan.")
                return

        if chat_id is None or not isinstance(text, str):
            return

        command = text.split(maxsplit=1)[0].split("@", maxsplit=1)[0].lower()
        if command == ID_COMMAND:
            if isinstance(user_id, int):
                self.send_message(
                    chat_id,
                    f"Your Telegram ID is: {user_id}",
                    message_id if isinstance(message_id, int) else None,
                )
            return

        if command == CHAT_ID_COMMAND:
            self.send_message(
                chat_id,
                f"Current chat ID is: {chat_id}",
                message_id if isinstance(message_id, int) else None,
            )
            return

        if command == POST_PREVIEW_COMMAND:
            if self.admin_user_id != user_id:
                self.send_message(chat_id, "❌ Perintah ini khusus admin.")
                return
            if not self.preview_chat_id:
                self.send_message(
                    chat_id,
                    "❌ PREVIEW_CHAT_ID belum dikonfigurasi. "
                    "Diperlukan Telegram chat ID grup/channel tujuan preview.",
                )
                return
            video_code = self.video_code_from_start(text)
            if not video_code:
                self.send_message(chat_id, "Gunakan format: /postpreview MC001")
                return
            self.pending_preview_codes[str(chat_id)] = video_code
            self.send_message(
                chat_id,
                f"Silakan kirim video preview untuk {video_code}. "
                "Video ini akan dipublikasikan sebagai preview saja.",
            )
            return

        if command == "/start":
            video_code = self.video_code_from_start(text)
            if video_code:
                user = message.get("from", {})
                user_id = user.get("id") if isinstance(user, dict) else None
                if isinstance(user_id, int) and self.has_channel_access(user_id):
                    self.send_message(
                        chat_id,
                        self.video_access_message(video_code),
                        message_id if isinstance(message_id, int) else None,
                        self.open_video_keyboard(video_code),
                    )
                else:
                    self.send_message(
                        chat_id,
                        VIDEO_DENIED_MESSAGE,
                        message_id if isinstance(message_id, int) else None,
                        self.access_keyboard(video_code),
                    )
                return
            self.send_message(
                chat_id,
                WELCOME_MESSAGE,
                message_id if isinstance(message_id, int) else None,
                self.access_keyboard(),
            )

    def handle_callback_query(self, callback_query: dict[str, Any]) -> None:
        callback_id = callback_query.get("id")
        if not isinstance(callback_id, str):
            return

        data = callback_query.get("data")
        video_code: str | None = None
        if data == CHECK_ACCESS_CALLBACK:
            pass
        elif isinstance(data, str) and data.startswith(f"{CHECK_ACCESS_CALLBACK}:"):
            candidate = data.split(":", maxsplit=1)[1].upper()
            if candidate not in VIDEO_CODES:
                self.answer_callback(callback_id, "Kode video tidak ditemukan.")
                return
            video_code = candidate
        else:
            self.answer_callback(callback_id)
            return

        user = callback_query.get("from", {})
        user_id = user.get("id") if isinstance(user, dict) else None
        callback_message = callback_query.get("message", {})
        chat = callback_message.get("chat", {}) if isinstance(callback_message, dict) else {}
        chat_id = chat.get("id") if isinstance(chat, dict) else None
        message_id = (
            callback_message.get("message_id")
            if isinstance(callback_message, dict)
            else None
        )

        if not isinstance(user_id, int) or chat_id is None or not isinstance(message_id, int):
            self.answer_callback(callback_id, "Unable to identify this check.")
            return

        if video_code is not None:
            target_matches_mc001 = (
                isinstance(callback_message, dict)
                and callback_message.get("text") == VIDEO_DENIED_MESSAGE
                and callback_message.get("reply_markup") == self.access_keyboard(video_code)
            )
            try:
                has_access = self.has_channel_access(user_id)
            except TelegramError:
                LOGGER.exception("MC001 access check failed; continuing polling.")
                try:
                    self.answer_callback(
                        callback_id,
                        "❌ Gagal memeriksa akses.",
                        show_alert=True,
                    )
                except TelegramError:
                    LOGGER.exception("Failed to acknowledge MC001 access error.")
                return

            try:
                if has_access:
                    self.answer_callback(callback_id)
                else:
                    self.answer_callback(
                        callback_id,
                        "❌ Kamu belum menjadi member.",
                        show_alert=True,
                    )
            except TelegramError:
                LOGGER.exception("Failed to acknowledge MC001 access callback.")
                return

            if not has_access and target_matches_mc001:
                return

            try:
                if has_access:
                    self.edit_message(
                        chat_id,
                        message_id,
                        self.video_access_message(video_code),
                        self.open_video_keyboard(video_code),
                    )
                else:
                    self.edit_message(
                        chat_id,
                        message_id,
                        VIDEO_DENIED_MESSAGE,
                        self.access_keyboard(video_code),
                    )
            except TelegramError as error:
                if self.is_message_not_modified_error(error):
                    return
                else:
                    LOGGER.exception("MC001 callback message update failed; continuing polling.")
            return

        has_access = self.has_channel_access(user_id)

        self.answer_callback(callback_id)
        if has_access:
            if video_code:
                self.edit_message(
                    chat_id,
                    message_id,
                    self.video_access_message(video_code),
                    self.open_video_keyboard(video_code),
                )
                return
            self.edit_message(
                chat_id,
                message_id,
                ACCESS_GRANTED_MESSAGE,
                {"inline_keyboard": []},
            )
        else:
            self.edit_message(
                chat_id,
                message_id,
                VIDEO_DENIED_MESSAGE if video_code else WELCOME_MESSAGE,
                self.access_keyboard(video_code),
            )

    def run(self) -> None:
        self.telegram("/deleteWebhook", {"drop_pending_updates": False})
        bot_info = self.telegram("/getMe")
        username = bot_info.get("username", "unknown") if isinstance(bot_info, dict) else "unknown"
        LOGGER.info("Bot connected as @%s. Waiting for messages.", username)
        missing_publisher_config = [
            name
            for name, value in (
                ("ADMIN_USER_ID", self.admin_user_id),
                ("PREVIEW_CHAT_ID", self.preview_chat_id),
            )
            if not value
        ]
        if missing_publisher_config:
            LOGGER.warning(
                "Preview publisher not ready; missing environment variable(s): %s.",
                ", ".join(missing_publisher_config),
            )
        else:
            LOGGER.info("Preview publisher configured.")

        polling_retry_delay = 1.0
        while self.running:
            try:
                updates = self.telegram(
                    "/getUpdates",
                    {
                        "offset": self.offset,
                        "timeout": 25,
                        "allowed_updates": ["message", "channel_post", "callback_query"],
                    },
                )
            except TelegramError as error:
                LOGGER.error(
                    "Polling /getUpdates failed; retrying in %.1fs: %s",
                    polling_retry_delay,
                    error,
                )
                time.sleep(polling_retry_delay)
                polling_retry_delay = min(polling_retry_delay * 2, 30.0)
                continue

            polling_retry_delay = 1.0
            if not isinstance(updates, list):
                LOGGER.error("Polling /getUpdates returned an invalid updates list; retrying.")
                time.sleep(polling_retry_delay)
                continue

            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    self.offset = max(self.offset, update_id + 1)
                message = update.get("message")
                if isinstance(message, dict):
                    try:
                        self.handle_message(message)
                    except TelegramError:
                        LOGGER.exception("Telegram rejected a message update; continuing polling.")
                channel_post = update.get("channel_post")
                if isinstance(channel_post, dict):
                    try:
                        self.handle_message(channel_post)
                    except TelegramError:
                        LOGGER.exception("Telegram rejected a channel post; continuing polling.")
                callback_query = update.get("callback_query")
                if isinstance(callback_query, dict):
                    try:
                        self.handle_callback_query(callback_query)
                    except TelegramError:
                        LOGGER.exception("Telegram rejected a callback update; continuing polling.")

    def stop(self, *_args: Any) -> None:
        LOGGER.info("Stopping bot.")
        self.running = False


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    bridge = ConnectorBridge.start()
    bot = TelegramBot(bridge)
    signal.signal(signal.SIGINT, bot.stop)
    signal.signal(signal.SIGTERM, bot.stop)

    try:
        bot.run()
    except KeyboardInterrupt:
        bot.stop()
    except TelegramError:
        LOGGER.exception("Bot stopped because Telegram returned an error.")
        return 1
    finally:
        bridge.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

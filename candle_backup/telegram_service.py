"""Telegram notifications for the candle-backup tool.

Sends formatted messages to a Telegram chat via the Telegram Bot HTTP
API directly. No third-party Telegram packages — only ``requests``,
already a dependency.

Design
------
* Best-effort: every public method catches its own errors and logs a
  warning. A Telegram failure never aborts the backup.
* Synchronous: the tool is a CLI, so there is no event loop to worry
  about. ``send()`` blocks for at most ``TELEGRAM_TIMEOUT_SECONDS``.
* HTML parse mode: dynamic values are escaped; bold labels and emoji
  are added by the formatter so the message renders cleanly.
"""

from __future__ import annotations

from html import escape
from typing import Any, Iterable

import requests

from candle_backup.config import settings
from candle_backup.logger import get_logger

logger = get_logger(__name__)

_TELEGRAM_API_BASE = "https://api.telegram.org"

# Level -> leading emoji. Unknown levels fall back to a neutral bullet.
_LEVEL_EMOJI: dict[str, str] = {
    "START": "🚀",
    "SUCCESS": "✅",
    "INFO": "ℹ️",
    "WARNING": "⚠️",
    "ERROR": "❌",
}


class TelegramService:
    """Thin wrapper around the Bot API ``sendMessage`` endpoint."""

    def __init__(self) -> None:
        token = (settings.telegram_bot_token or "").strip()
        chat_id = (settings.telegram_chat_id or "").strip()

        self._token = token
        self._chat_id = chat_id
        self._timeout = max(1, int(settings.telegram_timeout_seconds))
        self._enabled = bool(
            settings.telegram_enabled and token and chat_id
        )
        self._api_url = (
            f"{_TELEGRAM_API_BASE}/bot{token}/sendMessage"
            if token
            else ""
        )

        if self._enabled:
            logger.info(
                "Telegram notifications enabled; chat_id=%s",
                _mask(self._chat_id),
            )
        elif settings.telegram_enabled:
            logger.warning(
                "TELEGRAM_ENABLED=true but token and/or chat id missing; "
                "notifications will be skipped."
            )

    # ------------------------------------------------------------------ #
    # Introspection                                                      #
    # ------------------------------------------------------------------ #
    @property
    def enabled(self) -> bool:
        return self._enabled

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #
    def send(
        self,
        title: str,
        lines: Iterable[str] | None = None,
        *,
        level: str = "INFO",
    ) -> bool:
        """Send a structured multi-line notification. Never raises."""
        if not self._enabled:
            return False

        text = self._format_event(title, list(lines or []), level)
        return self._send_sync(text)

    # ------------------------------------------------------------------ #
    # Convenience wrappers                                               #
    # ------------------------------------------------------------------ #
    def notify_start(self, details: dict[str, Any] | None = None) -> bool:
        return self.send(
            "Candle Backup Started",
            self._kv_lines(details or {}),
            level="START",
        )

    def notify_success(self, details: dict[str, Any] | None = None) -> bool:
        return self.send(
            "Candle Backup Completed",
            self._kv_lines(details or {}),
            level="SUCCESS",
        )

    def notify_failure(
        self,
        error: str,
        details: dict[str, Any] | None = None,
    ) -> bool:
        lines = self._kv_lines(details or {})
        lines.append(f"<b>Error:</b> {escape(error)}")
        return self.send("Candle Backup Failed", lines, level="ERROR")

    # ------------------------------------------------------------------ #
    # Internals                                                          #
    # ------------------------------------------------------------------ #
    def _send_sync(self, text: str) -> bool:
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "disable_notification": False,
        }

        try:
            response = requests.post(
                self._api_url,
                json=payload,
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            logger.warning("Telegram request failed: %s", exc)
            return False

        if response.status_code != 200:
            logger.warning(
                "Telegram API non-200; status=%s body=%s",
                response.status_code,
                response.text[:300],
            )
            return False

        try:
            body = response.json()
        except ValueError:
            return True

        if not body.get("ok"):
            logger.warning("Telegram API returned ok=false: %s", body)
            return False

        return True

    @staticmethod
    def _format_event(
        title: str, lines: list[str], level: str
    ) -> str:
        emoji = _LEVEL_EMOJI.get(level.upper(), "•")
        header = f"{emoji} <b>{escape(title)}</b>"
        if not lines:
            return header
        return header + "\n\n" + "\n".join(lines)

    @staticmethod
    def _kv_lines(data: dict[str, Any]) -> list[str]:
        lines: list[str] = []
        for key, value in data.items():
            if value is None:
                continue
            label = escape(str(key).replace("_", " ").title())
            lines.append(f"<b>{label}:</b> {escape(str(value))}")
        return lines


def _mask(value: str) -> str:
    """Return a redacted form of a chat id for logs."""
    if len(value) <= 4:
        return "*" * len(value)
    return value[:2] + "*" * (len(value) - 4) + value[-2:]


telegram_service = TelegramService()
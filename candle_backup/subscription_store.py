"""Fetch the upstream subscriptions list.

Uses ``requests`` (synchronous) wrapped in ``asyncio.to_thread`` so the
event loop stays responsive. The in-memory list is replaced atomically
only after the full response has been validated, so a failed refresh
never clobbers previously loaded good data.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import requests

from candle_backup.config import settings
from candle_backup.logger import get_logger

logger = get_logger(__name__)


class SubscriptionStore:
    def __init__(self) -> None:
        self._items: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()
        self.last_refreshed_at: str | None = None
        self.last_error: str | None = None
        self.raw_count = 0
        self.duplicate_count = 0
        self.invalid_count = 0

    async def refresh(self, reason: str) -> dict[str, Any]:
        async with self._lock:
            logger.info("Subscription refresh started; reason=%s", reason)

            if not settings.subscriptions_api_url:
                raise RuntimeError("SUBSCRIPTIONS_API_URL is not configured")

            try:
                payload = await asyncio.to_thread(self._fetch_payload)

                if not isinstance(payload, dict):
                    raise ValueError("Upstream response must be a JSON object")

                active = payload.get("active", [])
                if not isinstance(active, list):
                    raise ValueError("Upstream field 'active' must be a list")

                seen: set[str] = set()
                unique_items: list[dict[str, Any]] = []
                invalid_count = 0
                duplicate_count = 0

                for item in active:
                    if not isinstance(item, dict):
                        invalid_count += 1
                        continue

                    instrument_key = item.get("instrument_key")
                    if (
                        not isinstance(instrument_key, str)
                        or not instrument_key.strip()
                    ):
                        invalid_count += 1
                        continue

                    if instrument_key in seen:
                        duplicate_count += 1
                        continue

                    seen.add(instrument_key)
                    unique_items.append(item)

                self._items = unique_items
                self.raw_count = len(active)
                self.duplicate_count = duplicate_count
                self.invalid_count = invalid_count
                self.last_refreshed_at = datetime.now(timezone.utc).isoformat()
                self.last_error = None

                logger.info(
                    "Subscription refresh completed; reason=%s raw=%d "
                    "unique=%d duplicates=%d invalid=%d",
                    reason,
                    self.raw_count,
                    len(self._items),
                    self.duplicate_count,
                    self.invalid_count,
                )
                return self.snapshot()
            except Exception as exc:
                self.last_error = str(exc)
                logger.exception("Subscription refresh failed; reason=%s", reason)
                raise

    def _fetch_payload(self) -> Any:
        response = requests.get(
            settings.subscriptions_api_url,
            headers={"Accept": "application/json"},
            timeout=settings.request_timeout_seconds,
        )
        response.raise_for_status()
        return response.json()

    def snapshot(self) -> dict[str, Any]:
        return {
            "active": self._items,
            "meta": {
                "unique_count": len(self._items),
                "raw_count": self.raw_count,
                "duplicate_count": self.duplicate_count,
                "invalid_count": self.invalid_count,
                "last_refreshed_at": self.last_refreshed_at,
                "source_url": settings.subscriptions_api_url,
                "last_error": self.last_error,
            },
        }


subscription_store = SubscriptionStore()
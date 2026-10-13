"""Upstox data helpers.

- ``fetch_historical`` uses the Upstox Python SDK (HistoryV3Api) in
  7-day chunks.
- ``fetch_intraday`` uses ``requests`` against the V3 REST endpoint
  directly, which keeps the call fully synchronous and lets
  ``asyncio.to_thread`` manage concurrency cleanly.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any
from urllib.parse import quote

import requests

from candle_backup.config import settings
from candle_backup.logger import get_logger

try:  # pragma: no cover - optional dependency
    import upstox_client  # type: ignore
except ImportError:  # pragma: no cover
    upstox_client = None  # type: ignore

logger = get_logger(__name__)

# Upstox HistoryV3 accepts at most ~7 days per request for minute data.
CHUNK_DAYS = 7

# V3 Intraday REST endpoint base.
_INTRADAY_BASE = "https://api.upstox.com/v3/historical-candle/intraday"


def _require_sdk() -> None:
    if upstox_client is None:
        raise RuntimeError(
            "upstox_client is not installed; add upstox-python-sdk to "
            "requirements.txt"
        )


def fetch_historical(
    instrument_key: str,
    from_date: date,
    to_date: date,
) -> list[dict[str, Any]]:
    """Fetch history in 7-day chunks via the SDK; returns [] on total failure."""
    _require_sdk()
    api = upstox_client.HistoryV3Api()
    collected: list[dict[str, Any]] = []

    cursor = from_date
    while cursor <= to_date:
        chunk_end = min(cursor + timedelta(days=CHUNK_DAYS - 1), to_date)
        try:
            response = api.get_historical_candle_data1(
                instrument_key,
                "minutes",
                "1",
                chunk_end.isoformat(),
                cursor.isoformat(),
            )
            collected.extend(extract_candles(response))
        except Exception as exc:
            logger.warning(
                "HistoryV3 historical fetch failed; instrument=%s "
                "from=%s to=%s error=%s",
                instrument_key,
                cursor,
                chunk_end,
                exc,
            )
        cursor = chunk_end + timedelta(days=1)

    return collected


def fetch_intraday(instrument_key: str) -> list[dict[str, Any]]:
    """Fetch today's 1-minute candles from the V3 REST endpoint."""
    encoded_key = quote(instrument_key, safe="")
    url = f"{_INTRADAY_BASE}/{encoded_key}/minutes/1"

    response = requests.get(
        url,
        headers={"Accept": "application/json"},
        timeout=settings.request_timeout_seconds,
    )

    if response.status_code != 200:
        response.raise_for_status()

    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Upstox intraday response must be a JSON object")

    return extract_candles(payload)


def extract_candles(response: Any) -> list[dict[str, Any]]:
    """Normalise an SDK object or a REST JSON payload into candle dicts."""
    data: Any = response

    if hasattr(data, "data"):
        data = data.data

    if hasattr(data, "to_dict"):
        try:
            data = data.to_dict()
        except Exception:
            pass

    if isinstance(data, dict):
        inner = data.get("data")
        if isinstance(inner, dict):
            raw = inner.get("candles") or []
        else:
            raw = data.get("candles") or []
    else:
        raw = getattr(data, "candles", []) or []

    candles: list[dict[str, Any]] = []
    for entry in raw:
        if isinstance(entry, dict):
            candles.append(entry)
        elif isinstance(entry, (list, tuple)) and len(entry) >= 5:
            candles.append(
                {
                    "timestamp": entry[0],
                    "open": entry[1],
                    "high": entry[2],
                    "low": entry[3],
                    "close": entry[4],
                    "volume": entry[5] if len(entry) > 5 else 0,
                    "oi": entry[6] if len(entry) > 6 else 0,
                }
            )
    return candles
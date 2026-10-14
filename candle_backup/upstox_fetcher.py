"""Upstox data helpers.

- ``fetch_historical`` uses ``HistoryV3Api.get_historical_candle_data1``
  in 7-day chunks.
- ``fetch_intraday`` uses ``HistoryV3Api.get_intra_day_candle_data``.

Both endpoints are public and require **no access token**. The SDK is
constructed bare (``HistoryV3Api()``) with no ``Configuration`` object,
matching the official documentation examples. Instrument discovery is
handled separately in ``instruments_service`` — this module only
fetches candles.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

from candle_backup.logger import get_logger

# Import the SDK twice:
#   * under TYPE_CHECKING  -> Pylance sees a real module and can resolve
#                             the SDK's classes during static analysis.
#   * at runtime           -> tolerant import; ``None`` if the SDK is
#                             missing so the tool can fail with a clear
#                             RuntimeError instead of ImportError.
if TYPE_CHECKING:  # pragma: no cover - type-checker only
    import upstox_client  # type: ignore
else:  # pragma: no cover - runtime
    try:
        import upstox_client  # type: ignore
    except ImportError:
        upstox_client = None  # type: ignore[assignment]

logger = get_logger(__name__)

CHUNK_DAYS = 7


def _require_sdk() -> None:
    if upstox_client is None:
        raise RuntimeError(
            "upstox_client is not installed; add upstox-python-sdk to "
            "requirements.txt"
        )


def _api() -> Any:
    """Return a bare ``HistoryV3Api`` instance.

    No ``Configuration`` and no ``access_token`` — the historical and
    intraday candle endpoints are public.
    """
    _require_sdk()
    return upstox_client.HistoryV3Api()


def fetch_historical(
    instrument_key: str,
    from_date: date,
    to_date: date,
) -> list[dict[str, Any]]:
    """Fetch history in 7-day chunks; returns [] when every chunk fails."""
    api = _api()
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
    """Today's 1-minute candles via ``get_intra_day_candle_data``."""
    api = _api()
    response = api.get_intra_day_candle_data(instrument_key, "minutes", "1")
    return extract_candles(response)


def extract_candles(response: Any) -> list[dict[str, Any]]:
    """Normalise an SDK object or REST JSON payload into candle dicts."""
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
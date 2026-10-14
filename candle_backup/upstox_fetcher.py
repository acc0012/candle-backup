"""Upstox data helpers.

- ``fetch_historical`` uses ``HistoryV3Api.get_historical_candle_data1``
  in 7-day chunks.
- ``fetch_intraday`` uses ``HistoryV3Api.get_intra_day_candle_data``.

Both endpoints are public and require **no access token**.

Per-request timeout
-------------------
Every SDK call is issued with a per-request timeout (default 60s,
configurable via ``UPSTOX_REQUEST_TIMEOUT_SECONDS``) so a single slow
upstream response can never stall the whole backup run. The timeout is
applied two ways for belt-and-braces reliability:

1. ``_request_timeout`` is passed to the SDK method. This is honored
   natively by the OpenAPI-generated ``upstox_client`` — the client
   reads ``_request_timeout`` from ``**kwargs`` and forwards it to
   urllib3.
2. As a defensive fallback, the default socket timeout on the
   underlying ``urllib3`` pool manager is also set when the API
   instance is constructed, so any call that somehow forgets
   ``_request_timeout`` still respects the limit.

The SDK is constructed with an explicit ``Configuration`` /
``ApiClient`` so we can patch the pool manager. If anything goes wrong
during that setup, we fall back to a bare ``HistoryV3Api()`` — the
per-call ``_request_timeout`` still protects us either way.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

from candle_backup.config import settings
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

try:  # pragma: no cover - optional, only used for the pool patch
    import urllib3  # type: ignore
except ImportError:  # pragma: no cover
    urllib3 = None  # type: ignore[assignment]

logger = get_logger(__name__)

CHUNK_DAYS = 7

# Hard fallbacks — used only if settings cannot be read for any reason.
_DEFAULT_REQUEST_TIMEOUT_SECONDS = 60.0
_DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0


# ---------------------------------------------------------------------------
# SDK access
# ---------------------------------------------------------------------------

def _require_sdk() -> None:
    if upstox_client is None:
        raise RuntimeError(
            "upstox_client is not installed; add upstox-python-sdk to "
            "requirements.txt"
        )


def _request_timeout_seconds() -> float:
    """Resolve the per-request read timeout, preferring settings."""
    try:
        value = float(settings.upstox_request_timeout_seconds)
        if value > 0:
            return value
    except Exception:
        pass
    return _DEFAULT_REQUEST_TIMEOUT_SECONDS


def _connect_timeout_seconds() -> float:
    """Short connect timeout so dead hosts fail fast."""
    try:
        value = float(settings.upstox_connect_timeout_seconds)
        if value > 0:
            return value
    except Exception:
        pass
    return _DEFAULT_CONNECT_TIMEOUT_SECONDS


def _api() -> Any:
    """Return a ``HistoryV3Api`` instance with the socket timeout set.

    No access token is required — both HistoryV3 endpoints are public.
    """
    _require_sdk()

    read_timeout = _request_timeout_seconds()
    connect_timeout = _connect_timeout_seconds()

    try:
        configuration = upstox_client.Configuration()
        api_client = upstox_client.ApiClient(configuration)

        # Defensive fallback: patch the default socket timeout on the
        # underlying urllib3 pool manager. Any call that somehow
        # bypasses ``_request_timeout`` still gets capped here.
        if urllib3 is not None:
            try:
                timeout = urllib3.Timeout(
                    connect=connect_timeout,
                    read=read_timeout,
                )
                pool_kw = api_client.rest_client.pool_manager.connection_pool_kw
                pool_kw["timeout"] = timeout
            except Exception:
                logger.debug(
                    "Could not patch urllib3 pool timeout; relying on "
                    "_request_timeout per call",
                    exc_info=True,
                )

        return upstox_client.HistoryV3Api(api_client)
    except Exception:
        # Fall back to the bare constructor if anything in the setup
        # above goes wrong. ``_request_timeout`` still protects us.
        logger.debug(
            "Falling back to bare HistoryV3Api()", exc_info=True
        )
        return upstox_client.HistoryV3Api()


def _call_kwargs() -> dict[str, Any]:
    """Per-call kwargs carrying the request timeout."""
    return {"_request_timeout": _request_timeout_seconds()}


# ---------------------------------------------------------------------------
# Public fetchers
# ---------------------------------------------------------------------------

def fetch_historical(
    instrument_key: str,
    from_date: date,
    to_date: date,
) -> list[dict[str, Any]]:
    """Fetch history in 7-day chunks; returns [] when every chunk fails.

    Every chunk carries its own per-request timeout, so a single slow
    instrument cannot hold up the whole run — a stalled chunk fails
    after ``UPSTOX_REQUEST_TIMEOUT_SECONDS`` and the loop moves on.
    """
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
                **_call_kwargs(),
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
    response = api.get_intra_day_candle_data(
        instrument_key,
        "minutes",
        "1",
        **_call_kwargs(),
    )
    return extract_candles(response)


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

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
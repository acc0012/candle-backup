# candle_backup/strike_range_service.py
"""Derive option strike ranges from live index prices.

Two data sources are used depending on the day of the week:

* **Monday–Friday (intraday mode)** — pull today's 1-minute intraday
  candles via Upstox ``HistoryV3Api.get_intra_day_candle_data`` (public,
  no access token). The reference close is picked based on the current
  IST time:

      09:15 <= now < 10:00   -> close of the FIRST candle of today
      10:00 <= now < 23:59   -> close of the LAST  candle of today
      otherwise              -> fall back to the FIRST candle

* **Saturday–Sunday (weekend / historical mode)** — pull the last
  market day's 1-minute candles via
  ``HistoryV3Api.get_historical_candle_data1``. The date is walked
  backwards (skipping weekends) until a day with **non-zero** candle
  closes is found, and the LAST candle close of that day is used.

In both cases a symmetric strike window is built around that close::

    min = floor((close - range_points) / rounding) * rounding
    max = ceil ((close + range_points) / rounding) * rounding

Defaults:
    NIFTY      ±500   rounded to 50
    SENSEX     ±1000  rounded to 100
    BANKNIFTY  ±1500  rounded to 100
    MIDCPNIFTY ±500   rounded to 25

Every run logs the selected window + mode + candle timestamp + close,
e.g.::

    [STRIKE-RANGE] NSE_INDEX|Nifty 50 | window=10:00-23:59 IST |
        mode=LAST candle | candle_ts=2026-10-23T14:32:00+05:30 |
        close=24832.10 | range=[24300.00, 25350.00]
"""

from __future__ import annotations

import math
from datetime import datetime, time as dt_time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import upstox_client

from candle_backup.config import settings
from candle_backup.logger import get_logger
from candle_backup.upstox_fetcher import fetch_intraday

logger = get_logger(__name__)

_MARKET_TZ = ZoneInfo("Asia/Kolkata")

# How many calendar days we're willing to walk back looking for a
# trading day that actually has non-zero candle data.
_HISTORICAL_LOOKBACK_DAYS = 10

# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _parse_hhmm(value: str, default: dt_time) -> dt_time:
    try:
        hh, mm = value.strip().split(":")
        return dt_time(int(hh), int(mm))
    except Exception:
        logger.warning(
            "Invalid HH:MM value %r; falling back to %s",
            value, default.strftime("%H:%M"),
        )
        return default

def _now_ist() -> datetime:
    return datetime.now(_MARKET_TZ)

def _is_weekday(dt: datetime) -> bool:
    """True for Mon–Fri (weekday() 0..4)."""
    return dt.weekday() < 5

# ---------------------------------------------------------------------------
# Rounding helpers
# ---------------------------------------------------------------------------

def _round_down(value: float, step: float) -> float:
    return math.floor(value / step) * step

def _round_up(value: float, step: float) -> float:
    return math.ceil(value / step) * step

def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

# ---------------------------------------------------------------------------
# Intraday candle selection (Mon–Fri path)
# ---------------------------------------------------------------------------

def _pick_candle_close(
    candles: list[dict[str, Any]],
    now_ist: datetime,
) -> tuple[float | None, str, str | None, str]:
    """Return ``(close, mode, candle_timestamp, window_label)``.

    ``mode`` is ``"FIRST"`` or ``"LAST"``; ``window_label`` is the
    human-readable time window the run falls into (e.g.
    ``"10:00-23:59 IST"``).
    """
    if not candles:
        return None, "NONE", None, "empty"

    market_open_t = _parse_hhmm(settings.market_open_time, dt_time(9, 15))
    cutoff_t = _parse_hhmm(
        settings.strike_range_mode_cutoff_time, dt_time(10, 0)
    )
    end_t = dt_time(23, 59)

    now_t = now_ist.time()

    if market_open_t <= now_t < cutoff_t:
        mode = "FIRST"
        window_label = (
            f"{market_open_t.strftime('%H:%M')}-"
            f"{cutoff_t.strftime('%H:%M')} IST"
        )
    elif cutoff_t <= now_t < end_t:
        mode = "LAST"
        window_label = f"{cutoff_t.strftime('%H:%M')}-23:59 IST"
    else:
        # Before market open (or late night) — data may not be today's.
        mode = "FIRST"
        window_label = (
            f"outside-window fallback "
            f"(now={now_t.strftime('%H:%M')}, "
            f"market opens {market_open_t.strftime('%H:%M')})"
        )

    # ISO-8601 timestamps sort lexicographically == chronologically.
    ordered = sorted(candles, key=lambda c: str(c.get("timestamp", "")))
    chosen = ordered[0] if mode == "FIRST" else ordered[-1]

    ts = chosen.get("timestamp")
    close = _safe_float(chosen.get("close"))
    if close is None:
        return None, mode, str(ts) if ts is not None else None, window_label

    return close, mode, str(ts) if ts is not None else None, window_label

# ---------------------------------------------------------------------------
# Historical candles (weekend / fallback path)
# ---------------------------------------------------------------------------

def _normalize_historical_candles(response: Any) -> list[dict[str, Any]]:
    """Convert an ``HistoryV3Api`` response into our dict-candle format."""
    raw = None

    # Prefer attribute access (upstox_client returns pydantic models),
    # then fall back to dict-like access.
    data = getattr(response, "data", None)
    if data is not None:
        raw = getattr(data, "candles", None)
    if raw is None and isinstance(response, dict):
        raw = (response.get("data") or {}).get("candles")

    if not raw:
        return []

    normalized: list[dict[str, Any]] = []
    for c in raw:
        if isinstance(c, (list, tuple)) and len(c) >= 5:
            # Upstox V3 order: [timestamp, open, high, low, close, volume, oi]
            normalized.append(
                {
                    "timestamp": c[0],
                    "open": c[1],
                    "high": c[2],
                    "low": c[3],
                    "close": c[4],
                    "volume": c[5] if len(c) > 5 else None,
                    "oi": c[6] if len(c) > 6 else None,
                }
            )
        elif isinstance(c, dict):
            normalized.append(c)

    return normalized

def _fetch_historical_candles(
    instrument_key: str,
    from_date: str,
    to_date: str,
) -> list[dict[str, Any]]:
    """Fetch 1-minute historical candles for a single day range.

    ``from_date`` and ``to_date`` are ``YYYY-MM-DD`` strings. Returns an
    empty list on any failure so callers can simply try an earlier date.
    """
    try:
        api_instance = upstox_client.HistoryV3Api()
        response = api_instance.get_historical_candle_data1(
            instrument_key,
            "minutes",
            "1",
            to_date,
            from_date,
        )
    except Exception:
        logger.exception(
            "Historical candle fetch failed; instrument=%s range=%s..%s",
            instrument_key, from_date, to_date,
        )
        return []

    return _normalize_historical_candles(response)

def _fetch_last_market_day_candles(
    instrument_key: str,
    now_ist: datetime,
    lookback_days: int = _HISTORICAL_LOOKBACK_DAYS,
) -> tuple[list[dict[str, Any]], str | None]:
    """Return ``(candles, date_str)`` for the most recent day with data.

    Walks backwards from ``now_ist`` day-by-day, skipping weekends, and
    returns the first day that yields **non-zero** close candles. That
    handles both Sat/Sun and NSE/BSE holidays.
    """
    for offset in range(lookback_days + 1):
        day = (now_ist - timedelta(days=offset)).date()
        if day.weekday() >= 5:  # skip Sat/Sun
            continue

        from_date = day.isoformat()
        to_date = (day + timedelta(days=1)).isoformat()
        candles = _fetch_historical_candles(
            instrument_key, from_date, to_date
        )

        non_zero = [
            c for c in candles
            if (_safe_float(c.get("close")) or 0.0) > 0.0
        ]
        if non_zero:
            logger.info(
                "[STRIKE-RANGE] %s | historical non-zero candles found "
                "for %s (raw=%d non_zero=%d)",
                instrument_key, from_date, len(candles), len(non_zero),
            )
            return non_zero, from_date

        logger.debug(
            "[STRIKE-RANGE] %s | no non-zero historical candles for %s "
            "(raw=%d) — walking back",
            instrument_key, from_date, len(candles),
        )

    return [], None

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_strike_range(
    instrument_key: str,
    range_points: float,
    rounding: float,
) -> tuple[float, float] | None:
    """Return ``(min_strike, max_strike)`` for ``instrument_key``.

    Data source depends on the day of the week:

    * Mon–Fri: today's intraday candles (FIRST/LAST based on current time).
    * Sat–Sun: the last market day's historical candles (LAST close).

    If the weekday intraday fetch yields nothing (e.g. Monday pre-open,
    holiday), it falls back to the historical last-market-day method.

    Returns ``None`` on any failure so the caller can fall back to the
    static range.
    """
    if range_points <= 0 or rounding <= 0:
        logger.warning(
            "Invalid strike range params for %s: range_points=%s "
            "rounding=%s — skipping dynamic computation",
            instrument_key,
            range_points,
            rounding,
        )
        return None

    now_ist = _now_ist()
    close: float | None = None
    mode = "NONE"
    candle_ts: str | None = None
    window_label = "empty"

    # ---- Mon–Fri: intraday path -------------------------------------------
    if _is_weekday(now_ist):
        try:
            candles = fetch_intraday(instrument_key)
        except Exception:
            logger.exception(
                "Intraday fetch failed while computing strike range; "
                "instrument=%s", instrument_key,
            )
            candles = []

        close, mode, candle_ts, window_label = _pick_candle_close(
            candles, now_ist
        )

        if close is None:
            logger.warning(
                "[STRIKE-RANGE] %s | weekday intraday empty "
                "(candles=%d) — falling back to historical "
                "last-market-day",
                instrument_key, len(candles),
            )

    # ---- Sat/Sun OR weekday-fallback: historical path ---------------------
    if close is None:
        hist_candles, ref_date = _fetch_last_market_day_candles(
            instrument_key, now_ist
        )
        if not hist_candles:
            logger.warning(
                "[STRIKE-RANGE] %s | window=%s | mode=%s | "
                "no usable candle close in intraday or historical "
                "lookback (%d days) — cannot compute range",
                instrument_key, window_label, mode,
                _HISTORICAL_LOOKBACK_DAYS,
            )
            return None

        # Use the LAST candle of the last market day.
        ordered = sorted(
            hist_candles, key=lambda c: str(c.get("timestamp", ""))
        )
        chosen = ordered[-1]
        candle_ts = (
            str(chosen.get("timestamp"))
            if chosen.get("timestamp") is not None else None
        )
        close = _safe_float(chosen.get("close"))
        mode = "LAST"
        window_label = f"historical last-market-day {ref_date}"

        if close is None:
            logger.warning(
                "[STRIKE-RANGE] %s | window=%s | mode=%s | "
                "chosen historical candle has no usable close "
                "(candle_ts=%s)",
                instrument_key, window_label, mode, candle_ts,
            )
            return None

    # ---- Build the strike window ------------------------------------------
    lo = _round_down(close - range_points, rounding)
    hi = _round_up(close + range_points, rounding)

    if lo >= hi:
        logger.warning(
            "[STRIKE-RANGE] %s | window=%s | mode=%s | "
            "degenerate range: close=%.2f lo=%.2f hi=%.2f",
            instrument_key, window_label, mode, close, lo, hi,
        )
        return None

    # ---- The explicit "which window / which candle" message ---------------
    logger.info(
        "[STRIKE-RANGE] %s | window=%s | mode=%s candle | "
        "candle_ts=%s | close=%.2f | ±%.0f (step %.0f) "
        "-> range=[%.0f, %.0f]",
        instrument_key,
        window_label,
        mode,
        candle_ts,
        close,
        range_points,
        rounding,
        lo,
        hi,
    )
    return lo, hi

def compute_dynamic_strike_ranges() -> dict[str, tuple[float, float]]:
    """Return ``{UNDERLYING: (min, max)}`` for every configured index.

    Underlyings whose fetch fails (both intraday and historical) are
    omitted — the caller should fall back to the static values in
    settings.
    """
    ranges: dict[str, tuple[float, float]] = {}

    specs = (
        # (instrument_key, underlying, range_points, rounding)
        (
            settings.nifty_index_instrument_key,
            settings.nifty_underlying.upper(),
            settings.nifty_strike_range_points,
            settings.nifty_strike_rounding,
        ),
        (
            settings.sensex_index_instrument_key,
            settings.sensex_underlying.upper(),
            settings.sensex_strike_range_points,
            settings.sensex_strike_rounding,
        ),
        (
            settings.banknifty_index_instrument_key,
            settings.banknifty_underlying.upper(),
            settings.banknifty_strike_range_points,
            settings.banknifty_strike_rounding,
        ),
        (
            settings.midcpnifty_index_instrument_key,
            settings.midcpnifty_underlying.upper(),
            settings.midcpnifty_strike_range_points,
            settings.midcpnifty_strike_rounding,
        ),
    )

    for instrument_key, underlying, range_points, rounding in specs:
        if not instrument_key or not underlying:
            continue
        result = compute_strike_range(instrument_key, range_points, rounding)
        if result is not None:
            ranges[underlying] = result
        else:
            logger.warning(
                "Could not compute dynamic strike range for %s "
                "(instrument_key=%s); caller will fall back to static",
                underlying, instrument_key,
            )

    return ranges
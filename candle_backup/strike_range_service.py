# candle_backup/strike_range_service.py
"""Derive option strike ranges from live index prices.

For each configured index (NIFTY, SENSEX) this module:

1. Fetches today's 1-minute intraday candles via Upstox ``HistoryV3Api``
   (``get_intra_day_candle_data``). No access token is required — this
   endpoint is public, same as the instrument archive.

2. Chooses the reference close **based on the current time**
   (``STRIKE_RANGE_MODE_CUTOFF_TIME``, default 10:00 IST):

       09:15 <= now < 10:00   -> close of the FIRST candle of today
       10:00 <= now < 23:59   -> close of the LAST  candle of today
       otherwise              -> fall back to the FIRST candle (with a
                                 warning; data is likely not today's)

3. Builds a symmetric strike window around that close:

       min = floor((close - range_points) / rounding) * rounding
       max = ceil ((close + range_points) / rounding) * rounding

   Defaults: NIFTY ±500 rounded to 50; SENSEX ±1000 rounded to 100.

The selected time window + mode + candle timestamp + close are logged
on **every** run, e.g.::

    [STRIKE-RANGE] NSE_INDEX|Nifty 50 | window=10:00-23:59 IST |
        mode=LAST candle | candle_ts=2026-10-23T14:32:00+05:30 |
        close=24832.10 | range=[24300.00, 25350.00]
"""

from __future__ import annotations

import math
from datetime import datetime, time as dt_time
from typing import Any
from zoneinfo import ZoneInfo

from candle_backup.config import settings
from candle_backup.logger import get_logger
from candle_backup.upstox_fetcher import fetch_intraday

logger = get_logger(__name__)

_MARKET_TZ = ZoneInfo("Asia/Kolkata")


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


# ---------------------------------------------------------------------------
# Rounding helpers
# ---------------------------------------------------------------------------

def _round_down(value: float, step: float) -> float:
    return math.floor(value / step) * step


def _round_up(value: float, step: float) -> float:
    return math.ceil(value / step) * step


# ---------------------------------------------------------------------------
# Candle selection
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
    try:
        close = float(chosen.get("close"))
    except (TypeError, ValueError):
        return None, mode, str(ts) if ts is not None else None, window_label

    return close, mode, str(ts) if ts is not None else None, window_label


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_strike_range(
    instrument_key: str,
    range_points: float,
    rounding: float,
) -> tuple[float, float] | None:
    """Return ``(min_strike, max_strike)`` for ``instrument_key``.

    Uses the FIRST or LAST intraday candle of the day depending on the
    current IST time; see the module docstring for the windows.
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

    try:
        candles = fetch_intraday(instrument_key)
    except Exception:
        logger.exception(
            "Intraday fetch failed while computing strike range; "
            "instrument=%s", instrument_key,
        )
        return None

    now_ist = _now_ist()
    close, mode, candle_ts, window_label = _pick_candle_close(
        candles, now_ist
    )

    if close is None:
        logger.warning(
            "[STRIKE-RANGE] %s | window=%s | mode=%s | "
            "no usable candle close (candles=%d) — cannot compute range",
            instrument_key, window_label, mode, len(candles),
        )
        return None

    lo = _round_down(close - range_points, rounding)
    hi = _round_up(close + range_points, rounding)

    if lo >= hi:
        logger.warning(
            "[STRIKE-RANGE] %s | window=%s | mode=%s | "
            "degenerate range: close=%.2f lo=%.2f hi=%.2f",
            instrument_key, window_label, mode, close, lo, hi,
        )
        return None

    # ---- The explicit "which window / which candle" message ----------
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

    Underlyings whose intraday fetch fails are omitted — the caller
    should fall back to the static values in settings.
    """
    ranges: dict[str, tuple[float, float]] = {}

    nifty = compute_strike_range(
        settings.nifty_index_instrument_key,
        settings.nifty_strike_range_points,
        settings.nifty_strike_rounding,
    )
    if nifty is not None:
        ranges[settings.nifty_underlying.upper()] = nifty

    sensex = compute_strike_range(
        settings.sensex_index_instrument_key,
        settings.sensex_strike_range_points,
        settings.sensex_strike_rounding,
    )
    if sensex is not None:
        ranges[settings.sensex_underlying.upper()] = sensex

    return ranges
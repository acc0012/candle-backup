"""Expiry checks for subscribed instruments.

An instrument is considered "expired" once the current time (in
``settings.market_timezone``) has passed ``settings.expiry_cutoff_time``
on the instrument's ``expiry`` date.

Instruments without an ``expiry`` field — index rows like
``NSE_INDEX|Nifty 50`` — are never treated as expired.

This module is intentionally dependency-free apart from ``config`` and
``logger`` so it can be imported from any stage of the pipeline.
"""

from __future__ import annotations

from datetime import date, datetime, time as dt_time
from typing import Any
from zoneinfo import ZoneInfo

from candle_backup.config import settings
from candle_backup.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Timezone + parsing helpers
# ---------------------------------------------------------------------------

def market_tz() -> ZoneInfo:
    """Return the configured market timezone (falls back to UTC)."""
    try:
        return ZoneInfo(settings.market_timezone)
    except Exception:
        return ZoneInfo("UTC")


def now_market_tz() -> datetime:
    """Current time in the configured market timezone."""
    return datetime.now(market_tz())


def parse_hhmm(value: str) -> dt_time:
    """Parse ``"HH:MM"`` into ``datetime.time``; fall back to 16:00."""
    try:
        hh, mm = value.strip().split(":")
        return dt_time(int(hh), int(mm))
    except Exception:
        logger.warning(
            "Invalid time value %r; falling back to 16:00", value
        )
        return dt_time(16, 0)


def parse_expiry_date(value: Any) -> date | None:
    """Parse ``"2026-10-13"`` (or an ISO datetime) into a date."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return date.fromisoformat(s[:10])
        except ValueError:
            try:
                return datetime.fromisoformat(
                    s.replace("Z", "+00:00")
                ).date()
            except ValueError:
                return None
    return None


# ---------------------------------------------------------------------------
# Public check
# ---------------------------------------------------------------------------

def is_expired(
    item: dict[str, Any], now: datetime | None = None
) -> bool:
    """Return True when the instrument's expiry moment has passed.

    The expiry moment is ``EXPIRY_CUTOFF_TIME`` (default 16:00) on the
    instrument's ``expiry`` date, evaluated in ``MARKET_TIMEZONE``.

    Instruments without a parseable ``expiry`` are never expired, so
    index rows and instrument types that do not carry an expiry field
    are always fetched.
    """
    expiry_date = parse_expiry_date(item.get("expiry"))
    if expiry_date is None:
        return False

    if now is None:
        now = now_market_tz()

    cutoff_time = parse_hhmm(settings.expiry_cutoff_time)
    cutoff_dt = datetime.combine(
        expiry_date, cutoff_time, tzinfo=market_tz()
    )
    return now >= cutoff_dt


def expiry_summary(item: dict[str, Any]) -> dict[str, Any]:
    """Small helper for logging / metadata — human-readable expiry info."""
    expiry_date = parse_expiry_date(item.get("expiry"))
    return {
        "expiry": expiry_date.isoformat() if expiry_date else None,
        "cutoff_time": settings.expiry_cutoff_time,
        "timezone": settings.market_timezone,
    }
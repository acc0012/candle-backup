"""Keep a rolling local cache of candles.

Job A — ``ensure_recent_candles`` (historical, last N days)
----------------------------------------------------------
1. For every instrument, build its on-disk path:
   ``data/<underlying>/<strike>_<strike_type>/historical.json``.
2. If the local file already covers the most recent market day inside
   the lookback window, skip it.
3. Otherwise, fetch the missing range from Upstox HistoryV3Api in
   7-day chunks.
4. Merge new candles with whatever was already stored, drop anything
   older than ``HISTORY_LOOKBACK_DAYS``, and rewrite the file atomically.

Job B — ``ensure_intraday_candles`` (today only)
------------------------------------------------
Runs only when the current time (in ``settings.market_timezone``) is a
weekday between ``settings.market_open_time`` and
``settings.market_close_time``. Uses the V3 intraday REST endpoint and
merges the result into the same ``historical.json``.

When ``INTRADAY_TEST_MODE=true`` the market-hours gate is bypassed
entirely: the intraday step runs regardless of the day or time. This is
useful for CI runs that happen before the market opens, and for manual
verification. Note that the upstream endpoint will still return an
empty list if there is no data for the current day — the flag only
controls whether we *attempt* the fetch, not whether data exists.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from candle_backup.config import settings
from candle_backup.instrument_paths import build_candle_path
from candle_backup.logger import get_logger
from candle_backup.upstox_fetcher import fetch_historical, fetch_intraday

logger = get_logger(__name__)


def _parse_hhmm(value: str) -> dt_time:
    try:
        hh, mm = value.strip().split(":")
        return dt_time(int(hh), int(mm))
    except Exception:
        logger.warning("Invalid time value '%s'; falling back to 00:00", value)
        return dt_time(0, 0)


class HistoricalCandleStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._semaphore: asyncio.Semaphore | None = None

        # Historical state.
        self.fetched_count = 0
        self.skipped_count = 0
        self.failed_count = 0
        self._processed_count = 0
        self._total_count = 0

        # Intraday state.
        self.intraday_updated_count = 0
        self.intraday_empty_count = 0
        self.intraday_failed_count = 0
        self._intraday_processed_count = 0
        self._intraday_total_count = 0

    # ------------------------------------------------------------------ #
    # Market hours helper                                                #
    # ------------------------------------------------------------------ #
    def _now_market_tz(self) -> datetime:
        try:
            tz = ZoneInfo(settings.market_timezone)
        except Exception:
            logger.exception(
                "Invalid MARKET_TIMEZONE=%s; falling back to UTC",
                settings.market_timezone,
            )
            tz = timezone.utc
        return datetime.now(tz)

    def is_market_hours(self) -> bool:
        """Return True if the current time is inside the market window.

        When ``INTRADAY_TEST_MODE`` is true, this always returns True so
        the intraday step runs regardless of day or time.
        """
        if settings.intraday_test_mode:
            return True

        now = self._now_market_tz()
        if now.weekday() >= 5:
            return False
        open_t = _parse_hhmm(settings.market_open_time)
        close_t = _parse_hhmm(settings.market_close_time)
        return open_t <= now.time() <= close_t

    # ------------------------------------------------------------------ #
    # Historical                                                         #
    # ------------------------------------------------------------------ #
    async def ensure_recent_candles(
        self,
        instruments: list[dict[str, Any]],
        reason: str = "backup",
    ) -> dict[str, Any]:
        async with self._lock:
            logger.info(
                "Historical candle job started; reason=%s instruments=%d "
                "concurrency=%d",
                reason,
                len(instruments),
                settings.history_max_concurrency,
            )
            self.fetched_count = 0
            self.skipped_count = 0
            self.failed_count = 0
            self._processed_count = 0

            lookback = max(1, settings.history_lookback_days)
            to_date = date.today()
            cutoff = to_date - timedelta(days=lookback)

            concurrency = max(1, settings.history_max_concurrency)
            self._semaphore = asyncio.Semaphore(concurrency)

            valid_instruments = [i for i in instruments if isinstance(i, dict)]
            self._total_count = len(valid_instruments)

            tasks = [
                asyncio.create_task(self._process_instrument(item, cutoff, to_date))
                for item in valid_instruments
            ]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=False)

            summary = {
                "reason": reason,
                "lookback_days": lookback,
                "concurrency": concurrency,
                "total": self._total_count,
                "processed": self._processed_count,
                "fetched": self.fetched_count,
                "skipped": self.skipped_count,
                "failed": self.failed_count,
            }
            logger.info("Historical candle job completed; %s", summary)
            return summary

    async def _process_instrument(
        self,
        item: dict[str, Any],
        cutoff: date,
        to_date: date,
    ) -> None:
        instrument_key = item.get("instrument_key")
        if not isinstance(instrument_key, str) or not instrument_key.strip():
            self.skipped_count += 1
            self._processed_count += 1
            return

        target_path = build_candle_path(item)
        existing = self._load_existing(target_path)

        latest = self._latest_candle_date(existing)
        expected_last = self._last_market_day(to_date - timedelta(days=1))

        if latest is not None and latest >= expected_last:
            self.skipped_count += 1
            self._processed_count += 1
            return

        assert self._semaphore is not None
        async with self._semaphore:
            try:
                candles = await asyncio.to_thread(
                    fetch_historical, instrument_key, cutoff, to_date
                )
            except Exception:
                self.failed_count += 1
                self._processed_count += 1
                logger.exception(
                    "Historical candle fetch failed; instrument=%s "
                    "range=%s..%s",
                    instrument_key,
                    cutoff.isoformat(),
                    to_date.isoformat(),
                )
                return

        merged = self._merge_and_trim(existing, candles, cutoff)
        self._save(target_path, merged)
        self.fetched_count += 1
        self._processed_count += 1

    # ------------------------------------------------------------------ #
    # Intraday                                                           #
    # ------------------------------------------------------------------ #
    async def ensure_intraday_candles(
        self,
        instruments: list[dict[str, Any]],
        reason: str = "backup",
    ) -> dict[str, Any]:
        async with self._lock:
            now_local = self._now_market_tz()
            test_mode = settings.intraday_test_mode

            if not self.is_market_hours():
                summary = {
                    "reason": reason,
                    "skipped": True,
                    "skip_reason": (
                        "outside market hours "
                        f"({settings.market_open_time}-"
                        f"{settings.market_close_time} "
                        f"{settings.market_timezone})"
                    ),
                    "now_market_tz": now_local.isoformat(),
                    "test_mode": test_mode,
                }
                logger.info("Intraday candle job skipped; %s", summary)
                return summary

            if test_mode:
                logger.info(
                    "Intraday candle job running in TEST MODE; "
                    "market-hours gate bypassed; now=%s",
                    now_local.isoformat(),
                )

            self.intraday_updated_count = 0
            self.intraday_empty_count = 0
            self.intraday_failed_count = 0
            self._intraday_processed_count = 0

            lookback = max(1, settings.history_lookback_days)
            cutoff = date.today() - timedelta(days=lookback)

            concurrency = max(1, settings.history_max_concurrency)
            self._semaphore = asyncio.Semaphore(concurrency)

            valid_instruments = [i for i in instruments if isinstance(i, dict)]
            self._intraday_total_count = len(valid_instruments)

            tasks = [
                asyncio.create_task(
                    self._process_intraday_instrument(item, cutoff)
                )
                for item in valid_instruments
            ]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=False)

            summary = {
                "reason": reason,
                "skipped": False,
                "test_mode": test_mode,
                "total": self._intraday_total_count,
                "processed": self._intraday_processed_count,
                "updated": self.intraday_updated_count,
                "empty": self.intraday_empty_count,
                "failed": self.intraday_failed_count,
            }
            logger.info("Intraday candle job completed; %s", summary)
            return summary

    async def _process_intraday_instrument(
        self,
        item: dict[str, Any],
        cutoff: date,
    ) -> None:
        instrument_key = item.get("instrument_key")
        if not isinstance(instrument_key, str) or not instrument_key.strip():
            self.intraday_empty_count += 1
            self._intraday_processed_count += 1
            return

        target_path = build_candle_path(item)
        existing = self._load_existing(target_path)

        assert self._semaphore is not None
        async with self._semaphore:
            try:
                candles = await asyncio.to_thread(fetch_intraday, instrument_key)
            except Exception:
                self.intraday_failed_count += 1
                self._intraday_processed_count += 1
                logger.exception(
                    "Intraday candle fetch failed; instrument=%s",
                    instrument_key,
                )
                return

        if not candles:
            self.intraday_empty_count += 1
            self._intraday_processed_count += 1
            return

        merged = self._merge_and_trim(existing, candles, cutoff)
        self._save(target_path, merged)
        self.intraday_updated_count += 1
        self._intraday_processed_count += 1

    # ------------------------------------------------------------------ #
    # Disk IO / freshness                                                #
    # ------------------------------------------------------------------ #
    def _load_existing(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("Unable to read existing candle file: %s", path)
            return []

        candles = payload.get("candles") if isinstance(payload, dict) else payload
        if not isinstance(candles, list):
            return []
        return [c for c in candles if isinstance(c, dict)]

    def _save(self, path: Path, candles: list[dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "count": len(candles),
            "candles": candles,
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _latest_candle_date(
        self, candles: list[dict[str, Any]]
    ) -> date | None:
        latest: date | None = None
        for candle in candles:
            day = self._candle_date(candle.get("timestamp"))
            if day is None:
                continue
            if latest is None or day > latest:
                latest = day
        return latest

    @staticmethod
    def _last_market_day(day: date) -> date:
        while day.weekday() >= 5:
            day -= timedelta(days=1)
        return day

    @staticmethod
    def _candle_date(ts: Any) -> date | None:
        if isinstance(ts, datetime):
            return ts.date()
        if isinstance(ts, date):
            return ts
        if isinstance(ts, str):
            try:
                return datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
            except ValueError:
                try:
                    return datetime.strptime(ts[:10], "%Y-%m-%d").date()
                except ValueError:
                    return None
        return None

    def _merge_and_trim(
        self,
        existing: list[dict[str, Any]],
        fresh: list[dict[str, Any]],
        cutoff: date,
    ) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        for candle in list(existing) + list(fresh):
            ts = candle.get("timestamp")
            if ts is None:
                continue
            day = self._candle_date(ts)
            if day is None or day < cutoff:
                continue
            merged[str(ts)] = candle
        return sorted(merged.values(), key=lambda c: str(c.get("timestamp", "")))


historical_candle_store = HistoricalCandleStore()
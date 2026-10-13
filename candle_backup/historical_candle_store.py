"""Keep a rolling local cache of candles.

Job A — ``ensure_recent_candles`` (historical, last N days)
----------------------------------------------------------
1. For every instrument, build its on-disk path:
   ``data/<underlying>/<strike>_<strike_type>/historical.json``.
2. When ``HISTORY_STRICT_COMPLETENESS=true`` (default), always fetch the
   full lookback window so the local file has every market day, with no
   internal gaps. This is the mode intended for a backup job.
   When it is ``false``, the old incremental skip applies: if the local
   file already contains data through the previous trading day, it is
   skipped.
3. Fetch the missing range from Upstox ``HistoryV3Api`` in 7-day chunks.
4. Merge new candles with whatever was already stored (fresh wins on
   duplicate timestamps), drop anything older than
   ``HISTORY_LOOKBACK_DAYS``, and rewrite the file atomically.

Job B — ``ensure_intraday_candles`` (today only)
------------------------------------------------
Runs only when the current time (in ``settings.market_timezone``) is a
weekday between ``settings.market_open_time`` and
``settings.market_close_time``. Uses the V3 intraday REST endpoint and
merges the result into the same ``historical.json``.

Logging
-------
Dict payloads (job summaries, skip summaries) are emitted as JSON via
``json_log``. When ``settings.instrument_errors_only`` is True, per-instrument
progress lines are suppressed and only errors raised while processing an
instrument are logged. Job-level start/finish summaries are always logged.
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
            strict = settings.history_strict_completeness

            logger.info(
                "Historical candle job started; reason=%s instruments=%d "
                "concurrency=%d strict_completeness=%s",
                reason,
                len(instruments),
                settings.history_max_concurrency,
                strict,
            )
            self.fetched_count = 0
            self.skipped_count = 0
            self.failed_count = 0
            self._processed_count = 0

            lookback = max(1, settings.history_lookback_days)
            overlap = max(0, settings.history_verify_overlap_days)
            to_date = date.today()
            # Extend the fetch window backwards by `overlap` days so we
            # re-verify the days immediately before the rolling window.
            # Upstox occasionally back-fills a late candle.
            cutoff = to_date - timedelta(days=lookback + overlap)

            concurrency = max(1, settings.history_max_concurrency)
            self._semaphore = asyncio.Semaphore(concurrency)

            valid_instruments = [i for i in instruments if isinstance(i, dict)]
            self._total_count = len(valid_instruments)

            tasks = [
                asyncio.create_task(
                    self._process_instrument(item, cutoff, to_date, strict)
                )
                for item in valid_instruments
            ]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=False)

            summary = {
                "reason": reason,
                "lookback_days": lookback,
                "overlap_days": overlap,
                "strict_completeness": strict,
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
        strict: bool,
    ) -> None:
        instrument_key = item.get("instrument_key")
        if not isinstance(instrument_key, str) or not instrument_key.strip():
            self.skipped_count += 1
            self._processed_count += 1
            return

        target_path = build_candle_path(item)
        existing = self._load_existing(target_path)

        # ---- Decide whether to fetch -----------------------------------
        if not strict:
            # Incremental mode: skip if we already have data through the
            # previous trading day. Fast, but can hide internal gaps.
            latest = self._latest_candle_date(existing)
            expected_last = self._last_market_day(to_date - timedelta(days=1))
            if latest is not None and latest >= expected_last:
                self.skipped_count += 1
                self._processed_count += 1
                return
        # In strict mode we always fetch. A missing check is cheaper than
        # a missed trading day.

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

        # Even a successful call may return an empty list (a fully-holiday
        # range, or an upstream hiccup). Keep whatever is on disk.
        if not candles and not existing:
            self.failed_count += 1
            self._processed_count += 1
            logger.warning(
                "Historical candle fetch returned no data; instrument=%s "
                "range=%s..%s (no existing local data either)",
                instrument_key,
                cutoff.isoformat(),
                to_date.isoformat(),
            )
            return

        merged = self._merge_and_trim(existing, candles, cutoff)
        self._save(target_path, merged)
        self.fetched_count += 1
        self._processed_count += 1

        # Compact completeness report for this instrument: how many
        # weekdays in the window have candles vs how many are expected.
        expected_days = self._count_weekdays(cutoff, to_date)
        present_days = self._count_weekdays_present(merged, cutoff, to_date)
        logger.debug(
            "Historical fetch done; instrument=%s candles=%d new=%d "
            "weekdays_present=%d/%d",
            instrument_key,
            len(merged),
            len(candles),
            present_days,
            expected_days,
        )

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
                    "test_mode": settings.intraday_test_mode,
                }
                logger.info("Intraday candle job skipped; %s", summary)
                return summary

            if settings.intraday_test_mode:
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
                "test_mode": settings.intraday_test_mode,
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

    # ------------------------------------------------------------------ #
    # Completeness helpers                                               #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _count_weekdays(start: date, end: date) -> int:
        """Number of Mon-Fri days in [start, end] (inclusive)."""
        if end < start:
            return 0
        n = 0
        cursor = start
        while cursor <= end:
            if cursor.weekday() < 5:
                n += 1
            cursor += timedelta(days=1)
        return n

    def _count_weekdays_present(
        self,
        candles: list[dict[str, Any]],
        start: date,
        end: date,
    ) -> int:
        """Number of Mon-Fri days in [start, end] that have at least one candle."""
        present: set[date] = set()
        for candle in candles:
            d = self._candle_date(candle.get("timestamp"))
            if d is None or d < start or d > end:
                continue
            if d.weekday() < 5:
                present.add(d)
        return len(present)

    def _merge_and_trim(
        self,
        existing: list[dict[str, Any]],
        fresh: list[dict[str, Any]],
        cutoff: date,
    ) -> list[dict[str, Any]]:
        """Merge ``existing`` and ``fresh``; fresh wins on duplicate ts.

        Anything older than ``cutoff`` is dropped.
        """
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
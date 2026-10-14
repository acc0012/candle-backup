"""Keep a rolling local cache of candles + EMA crosses.

Job A — ``ensure_recent_candles`` (historical, last N days)
Job B — ``ensure_intraday_candles`` (today only)
Job C — ``compute_ema_crosses`` (writes ema_crosses.json)

The three jobs write into the same per-instrument directory:

    data/<underlying>/<strike>_<type>/candles.json
    data/<underlying>/<strike>_<type>/ema_crosses.json

``ensure_candles_and_emas`` runs all three in order.

Intraday window
---------------
The intraday fetch is **time-window gated**, not market-hours gated.
It runs whenever the current time in ``MARKET_TIMEZONE`` is inside

    [ MARKET_OPEN_TIME (default 09:15) , 23:59 )

so an end-of-day job at 17:00 IST still pulls the full day's candles,
and a late-evening retry up to 23:59 is allowed. Before 09:15 the
market has not opened yet (no candles for today); from 00:00 the
previous day is already covered by the historical fetch.

``INTRADAY_TEST_MODE=true`` remains as an escape hatch for manual runs
outside the window (e.g. weekend verification).
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from candle_backup.config import settings
from candle_backup.ema import detect_crosses
from candle_backup.instrument_paths import (
    build_candle_path,
    build_ema_path,
)
from candle_backup.logger import get_logger
from candle_backup.upstox_fetcher import fetch_historical, fetch_intraday

logger = get_logger(__name__)

# End of the intraday fetch window, exclusive. 23:59 in MARKET_TIMEZONE.
_INTRADAY_WINDOW_END = dt_time(23, 59)


def _parse_hhmm(value: str) -> dt_time:
    try:
        hh, mm = value.strip().split(":")
        return dt_time(int(hh), int(mm))
    except Exception:
        logger.warning("Invalid time value '%s'; falling back to 00:00", value)
        return dt_time(0, 0)


def _json_default(obj: Any) -> str:
    """Fallback serializer for datetimes/dates and anything else."""
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return str(obj)


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

        # EMA state.
        self.ema_files_written = 0
        self.ema_total_crosses = 0
        self.ema_failed_count = 0

    # ------------------------------------------------------------------ #
    # Time helpers                                                        #
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
        """True when now is inside the exchange trading session."""
        now = self._now_market_tz()
        if now.weekday() >= 5:
            return False
        open_t = _parse_hhmm(settings.market_open_time)
        close_t = _parse_hhmm(settings.market_close_time)
        return open_t <= now.time() <= close_t

    def _intraday_window_open(self, now: datetime | None = None) -> bool:
        """True when the intraday fetch is allowed to run.

        Window is ``MARKET_OPEN_TIME`` (default 09:15) inclusive up to
        ``23:59`` exclusive, evaluated in ``MARKET_TIMEZONE``. This is
        wider than ``is_market_hours()`` on purpose so an end-of-day
        job at 17:00 IST still pulls the complete trading day. The
        day-of-week is intentionally not checked here — Upstox simply
        returns nothing on non-trading days, and we want the same code
        path to run on weekends during manual verification.
        """
        now = now or self._now_market_tz()
        start = _parse_hhmm(settings.market_open_time)
        return start <= now.time() < _INTRADAY_WINDOW_END

    # ------------------------------------------------------------------ #
    # Top-level orchestrator                                              #
    # ------------------------------------------------------------------ #
    async def ensure_candles_and_emas(
        self,
        instruments: list[dict[str, Any]],
        reason: str = "backup",
    ) -> dict[str, Any]:
        hist = await self.ensure_recent_candles(instruments, reason=reason)
        intra = await self.ensure_intraday_candles(instruments, reason=reason)
        ema = self.compute_ema_crosses(instruments, reason=reason)
        return {
            "historical": hist,
            "intraday": intra,
            "ema_crosses": ema,
        }

    # ------------------------------------------------------------------ #
    # Historical                                                          #
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

        if not strict:
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

    # ------------------------------------------------------------------ #
    # Intraday                                                            #
    # ------------------------------------------------------------------ #
    async def ensure_intraday_candles(
        self,
        instruments: list[dict[str, Any]],
        reason: str = "backup",
    ) -> dict[str, Any]:
        async with self._lock:
            now_local = self._now_market_tz()
            window_open = self._intraday_window_open(now_local)

            # Skip only when we are outside the [09:15, 23:59) window
            # AND the manual override is off. Inside the window the
            # fetch is mandatory regardless of INTRADAY_TEST_MODE.
            if not window_open and not settings.intraday_test_mode:
                summary = {
                    "reason": reason,
                    "skipped": True,
                    "skip_reason": (
                        "outside intraday window "
                        f"({settings.market_open_time}-23:59 "
                        f"{settings.market_timezone})"
                    ),
                    "now_market_tz": now_local.isoformat(),
                    "test_mode": settings.intraday_test_mode,
                }
                logger.info("Intraday candle job skipped; %s", summary)
                return summary

            if window_open and not self.is_market_hours():
                logger.info(
                    "Intraday candle job running outside market hours "
                    "but inside the 09:15-23:59 window; now=%s",
                    now_local.isoformat(),
                )
            elif settings.intraday_test_mode and not window_open:
                logger.info(
                    "Intraday candle job running in TEST MODE; "
                    "window gate bypassed; now=%s",
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
                "window_open": window_open,
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
                candles = await asyncio.to_thread(
                    fetch_intraday, instrument_key
                )
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
    # EMA crosses                                                         #
    # ------------------------------------------------------------------ #
    def compute_ema_crosses(
        self,
        instruments: list[dict[str, Any]],
        reason: str = "backup",
    ) -> dict[str, Any]:
        """Load each candle file, detect EMA crosses, write ema_crosses.json."""
        fast = settings.ema_fast_period
        slow = settings.ema_slow_period

        self.ema_files_written = 0
        self.ema_total_crosses = 0
        self.ema_failed_count = 0

        for item in instruments:
            if not isinstance(item, dict):
                continue
            instrument_key = item.get("instrument_key")
            if not isinstance(instrument_key, str) or not instrument_key.strip():
                continue

            candle_path = build_candle_path(item)
            candles = self._load_existing(candle_path)
            if not candles:
                continue

            try:
                crosses = detect_crosses(candles, fast, slow)
            except Exception:
                self.ema_failed_count += 1
                logger.exception(
                    "EMA cross computation failed; instrument=%s",
                    instrument_key,
                )
                continue

            payload = {
                "instrument_key": instrument_key,
                "underlying": item.get("underlying_symbol")
                or item.get("underlying_key"),
                "strike_price": item.get("strike_price"),
                "strike_type": item.get("instrument_type"),
                "expiry": item.get("expiry"),
                "trading_symbol": item.get("trading_symbol"),
                "fast_period": fast,
                "slow_period": slow,
                "candle_count": len(candles),
                "cross_count": len(crosses),
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "crosses": crosses,
            }
            try:
                self._save_ema(build_ema_path(item), payload)
                self.ema_files_written += 1
                self.ema_total_crosses += len(crosses)
            except Exception:
                self.ema_failed_count += 1
                logger.exception(
                    "EMA file write failed; instrument=%s path=%s",
                    instrument_key,
                    build_ema_path(item),
                )

        summary = {
            "reason": reason,
            "fast_period": fast,
            "slow_period": slow,
            "files_written": self.ema_files_written,
            "total_crosses": self.ema_total_crosses,
            "failed": self.ema_failed_count,
        }
        logger.info("EMA cross job completed; %s", summary)
        return summary

    # ------------------------------------------------------------------ #
    # Disk IO / freshness                                                 #
    # ------------------------------------------------------------------ #
    def _load_existing(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("Unable to read existing candle file: %s", path)
            return []

        candles = (
            payload.get("candles") if isinstance(payload, dict) else payload
        )
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
        tmp.write_text(
            json.dumps(payload, indent=2, default=_json_default),
            encoding="utf-8",
        )
        tmp.replace(path)

    def _save_ema(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, indent=2, default=_json_default),
            encoding="utf-8",
        )
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
                return datetime.fromisoformat(
                    ts.replace("Z", "+00:00")
                ).date()
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
        return sorted(
            merged.values(), key=lambda c: str(c.get("timestamp", ""))
        )


historical_candle_store = HistoricalCandleStore()
# candle_backup/historical_candle_store.py
"""Keep a rolling local cache of candles + EMA crosses + SR levels.

Job A — ``ensure_recent_candles``     (historical, last N days)
Job B — ``ensure_intraday_candles``   (today only)
Job C — ``compute_ema_crosses``       (writes ema_crosses.json)
Job D — ``compute_sr_levels``         (writes sr_levels.json)

The four jobs write into the same per-instrument directory::

    data/<underlying>/<strike>_<type>/candles.json
    data/<underlying>/<strike>_<type>/ema_crosses.json
    data/<underlying>/<strike>_<type>/sr_levels.json

``ensure_candles_and_emas`` runs all four in order.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from candle_backup.config import settings
from candle_backup.ema import EmaValidationConfig, detect_crosses
from candle_backup.instrument_paths import (
    build_candle_path,
    build_ema_path,
    build_sr_path,
)
from candle_backup.logger import get_logger
from candle_backup.sr_levels import update_sr_levels_for_instrument
from candle_backup.upstox_fetcher import fetch_historical, fetch_intraday

logger = get_logger(__name__)

_INTRADAY_WINDOW_END = dt_time(23, 59)

def _parse_hhmm(value: str) -> dt_time:
    try:
        hh, mm = value.strip().split(":")
        return dt_time(int(hh), int(mm))
    except Exception:
        logger.warning("Invalid time value '%s'; falling back to 00:00", value)
        return dt_time(0, 0)

def _json_default(obj: Any) -> str:
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
        self.ema_valid_crosses = 0
        self.ema_failed_count = 0

        # SR state.
        self.sr_files_written = 0
        self.sr_total_levels = 0
        self.sr_new_levels = 0
        self.sr_failed_count = 0

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
        now = self._now_market_tz()
        if now.weekday() >= 5:
            return False
        open_t = _parse_hhmm(settings.market_open_time)
        close_t = _parse_hhmm(settings.market_close_time)
        return open_t <= now.time() <= close_t

    def _intraday_window_open(self, now: datetime | None = None) -> bool:
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
        sr = self.compute_sr_levels(instruments, reason=reason)
        return {
            "historical": hist,
            "intraday": intra,
            "ema_crosses": ema,
            "sr_levels": sr,
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
        # ---- Unchanged from the current file ------------------------
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
    # Intraday (unchanged)                                                #
    # ------------------------------------------------------------------ #
    async def ensure_intraday_candles(
        self,
        instruments: list[dict[str, Any]],
        reason: str = "backup",
    ) -> dict[str, Any]:
        # ---- Full body unchanged from current file ------------------
        async with self._lock:
            now_local = self._now_market_tz()
            window_open = self._intraday_window_open(now_local)

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
        # ---- Unchanged from current file ----------------------------
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
    # EMA crosses (unchanged body)                                        #
    # ------------------------------------------------------------------ #
    def compute_ema_crosses(
        self,
        instruments: list[dict[str, Any]],
        reason: str = "backup",
    ) -> dict[str, Any]:
        # ---- Full body unchanged from current file ------------------
        fast = settings.ema_fast_period
        slow = settings.ema_slow_period

        cfg = EmaValidationConfig(
            enable_validation=settings.ema_enable_validation,
            min_validation_score=settings.ema_min_validation_score,
            use_trend_filter=settings.ema_use_trend_filter,
            use_separation_filter=settings.ema_use_separation_filter,
            use_slope_filter=settings.ema_use_slope_filter,
            use_volume_filter=settings.ema_use_volume_filter,
            use_body_filter=settings.ema_use_body_filter,
            use_distance_filter=settings.ema_use_distance_filter,
            trend_fast_period=settings.ema_trend_fast_period,
            trend_slow_period=settings.ema_trend_slow_period,
            atr_period=settings.ema_atr_period,
            sep_atr_min=settings.ema_sep_atr_min,
            slope_lookback=settings.ema_slope_lookback,
            volume_lookback=settings.ema_volume_lookback,
            volume_mult=settings.ema_volume_mult,
            distance_atr_max=settings.ema_distance_atr_max,
        )

        self.ema_files_written = 0
        self.ema_total_crosses = 0
        self.ema_valid_crosses = 0
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
                crosses = detect_crosses(candles, fast, slow, validation=cfg)
            except Exception:
                self.ema_failed_count += 1
                logger.exception(
                    "EMA cross computation failed; instrument=%s",
                    instrument_key,
                )
                continue

            valid_count = sum(1 for c in crosses if c.get("valid"))

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
                "valid_cross_count": valid_count,
                "validation": {
                    "enabled": cfg.enable_validation,
                    "min_score": cfg.min_validation_score,
                    "filters": {
                        "trend": cfg.use_trend_filter,
                        "separation": cfg.use_separation_filter,
                        "slope": cfg.use_slope_filter,
                        "volume": cfg.use_volume_filter,
                        "body": cfg.use_body_filter,
                        "distance": cfg.use_distance_filter,
                    },
                },
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "crosses": crosses,
            }
            try:
                self._save_ema(build_ema_path(item), payload)
                self.ema_files_written += 1
                self.ema_total_crosses += len(crosses)
                self.ema_valid_crosses += valid_count
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
            "valid_crosses": self.ema_valid_crosses,
            "failed": self.ema_failed_count,
        }
        logger.info("EMA cross job completed; %s", summary)
        return summary

    # ------------------------------------------------------------------ #
    # Support / Resistance levels (NEW)                                   #
    # ------------------------------------------------------------------ #
    def compute_sr_levels(
        self,
        instruments: list[dict[str, Any]],
        reason: str = "backup",
    ) -> dict[str, Any]:
        """Load each candle file, detect pivots, write sr_levels.json."""
        if not settings.sr_enabled:
            summary = {
                "reason": reason,
                "enabled": False,
                "files_written": 0,
                "total_levels": 0,
                "new_levels": 0,
                "failed": 0,
            }
            logger.info("SR levels job skipped (SR_ENABLED=false); %s", summary)
            return summary

        left_bars = max(1, settings.sr_left_bars)
        right_bars = max(1, settings.sr_right_bars)

        self.sr_files_written = 0
        self.sr_total_levels = 0
        self.sr_new_levels = 0
        self.sr_failed_count = 0

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

            sr_path = build_sr_path(item)
            try:
                result = update_sr_levels_for_instrument(
                    instrument_key=instrument_key,
                    candles=candles,
                    path=sr_path,
                    left_bars=left_bars,
                    right_bars=right_bars,
                )
            except Exception:
                self.sr_failed_count += 1
                logger.exception(
                    "SR level update failed; instrument=%s", instrument_key
                )
                continue

            if result.get("error"):
                self.sr_failed_count += 1
                logger.warning(
                    "SR level update reported an error; instrument=%s "
                    "error=%s",
                    instrument_key, result["error"],
                )
                continue

            if result.get("written"):
                self.sr_files_written += 1
                self.sr_total_levels += int(result.get("total") or 0)
                self.sr_new_levels += int(result.get("new") or 0)

        summary = {
            "reason": reason,
            "enabled": True,
            "left_bars": left_bars,
            "right_bars": right_bars,
            "files_written": self.sr_files_written,
            "total_levels": self.sr_total_levels,
            "new_levels": self.sr_new_levels,
            "failed": self.sr_failed_count,
        }
        logger.info("SR level job completed; %s", summary)
        return summary

    # ------------------------------------------------------------------ #
    # Disk IO / freshness (unchanged)                                     #
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
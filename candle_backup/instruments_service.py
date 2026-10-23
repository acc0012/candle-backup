# candle_backup/instruments_service.py
"""Discover option instruments from Upstox's free public archive.

Downloads:

    https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz

The archive is cached on disk per-day so repeated runs don't re-download.
No authentication is required.

Returned instruments include two categories:

1. The main index rows themselves (``NSE_INDEX|Nifty 50`` and
   ``BSE_INDEX|SENSEX`` by default) so their candle/EMA series are backed
   up under ``data/index/<name>/``.
2. The nearest-expiry CE/PE options per underlying, filtered to a
   **dynamic strike window** derived from the index's intraday candles
   (see ``strike_range_service``).

Strike-range resolution is a three-tier fallback per underlying:

    Tier 1  dynamic range  (from first/last intraday candle)
    Tier 2  static range   (NIFTY_STRIKE_MIN/MAX, SENSEX_STRIKE_MIN/MAX)
    Tier 3  auto-detected range (min..max of every active CE/PE strike
            for that underlying in the archive)

If Tier 1 fails, Tier 2 is tried; if Tier 2 also yields **zero** option
contracts, Tier 3 kicks in so the snapshot always contains real option
strikes whenever the archive carries any.

Every discovery run writes a snapshot of the final instrument list to
``settings.all_instruments_file`` via
``all_instruments_service.save_all_instruments``.
"""

from __future__ import annotations

import gzip
import json
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

from candle_backup.all_instruments_service import save_all_instruments
from candle_backup.config import settings
from candle_backup.logger import get_logger
from candle_backup.strike_range_service import compute_dynamic_strike_ranges

logger = get_logger(__name__)

_MARKET_TZ = ZoneInfo("Asia/Kolkata")
_DEFAULT_TYPES: tuple[str, ...] = ("CE", "PE")

# ---------------------------------------------------------------------------
# Download + cache
# ---------------------------------------------------------------------------

def _cache_path() -> Path:
    settings.instruments_cache_dir.mkdir(parents=True, exist_ok=True)
    return settings.instruments_cache_dir / "instruments.json.gz"


def _cache_is_fresh(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=_MARKET_TZ)
        return mtime.date() == datetime.now(_MARKET_TZ).date()
    except Exception:
        return False


def download_instruments(force: bool = False) -> list[dict[str, Any]]:
    """Return the parsed instrument list, using a same-day disk cache."""
    cache = _cache_path()

    if not force and _cache_is_fresh(cache):
        try:
            with gzip.open(cache, "rt", encoding="utf-8") as f:
                data = json.load(f)
            logger.info(
                "Loaded %d instruments from cache: %s", len(data), cache
            )
            return data
        except Exception:
            logger.exception("Cache read failed; re-downloading")

    url = settings.instruments_url
    logger.info("Downloading instruments archive from %s", url)

    response = requests.get(
        url,
        stream=True,
        timeout=max(60, settings.request_timeout_seconds * 4),
    )
    response.raise_for_status()

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".json.gz")
    tmp_file = Path(tmp_path)
    try:
        with tmp_file.open("wb") as f:
            for chunk in response.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)

        with gzip.open(tmp_file, "rt", encoding="utf-8") as f:
            data = json.load(f)

        try:
            shutil.copyfile(tmp_file, cache)
        except Exception:
            logger.exception("Unable to update instrument cache at %s", cache)

        logger.info(
            "Loaded %d instruments from archive (cached at %s)",
            len(data),
            cache,
        )
        return data
    finally:
        try:
            tmp_file.unlink(missing_ok=True)
        except Exception:
            pass

# ---------------------------------------------------------------------------
# Expiry parsing
# ---------------------------------------------------------------------------

def _parse_expiry(value: Any) -> datetime | None:
    """Parse expiry from epoch ms or ISO string; returns IST-aware dt."""
    if value is None:
        return None

    try:
        ts = float(value)
        if ts > 1e11:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=_MARKET_TZ)
    except (TypeError, ValueError, OSError):
        pass

    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_MARKET_TZ)
            return dt.astimezone(_MARKET_TZ)
        except ValueError:
            try:
                return datetime.strptime(s[:10], "%Y-%m-%d").replace(
                    tzinfo=_MARKET_TZ
                )
            except ValueError:
                return None
    return None

# ---------------------------------------------------------------------------
# Underlying matching
# ---------------------------------------------------------------------------

def _norm(value: Any) -> str:
    return str(value or "").strip().upper().replace("_", " ")


def _first_token(value: str) -> str:
    parts = value.split()
    return parts[0] if parts else value


def _matches_underlying(item: dict[str, Any], target: str) -> bool:
    t_norm = _norm(target)
    t_head = _first_token(t_norm)

    for key in ("underlying_symbol", "name", "asset_symbol", "underlying_key"):
        raw = _norm(item.get(key))
        if not raw:
            continue
        head = _first_token(raw)
        if raw == t_norm or head == t_head:
            return True
    return False

# ---------------------------------------------------------------------------
# Strike helpers
# ---------------------------------------------------------------------------

def _strike_of(item: dict[str, Any]) -> float | None:
    try:
        return float(item.get("strike_price"))
    except (TypeError, ValueError):
        return None


def _now_ist() -> datetime:
    return datetime.now(_MARKET_TZ)


def _is_active_option(
    item: dict[str, Any],
    target: str,
    now: datetime,
    cutoff_today: datetime,
) -> bool:
    """True if ``item`` is a currently-active CE/PE for ``target``."""
    if not isinstance(item, dict):
        return False
    if _norm(item.get("instrument_type")) not in _DEFAULT_TYPES:
        return False
    if not _matches_underlying(item, target):
        return False
    exp = _parse_expiry(item.get("expiry"))
    if exp is None:
        return False
    if exp.date() < now.date():
        return False
    if exp.date() == now.date() and now >= cutoff_today:
        return False
    if _strike_of(item) is None:
        return False
    return True


def _auto_detect_strike_range(
    instruments: list[dict[str, Any]],
    target: str,
    now: datetime | None = None,
) -> tuple[float, float] | None:
    """Return ``(min_strike, max_strike)`` of every active CE/PE for ``target``.

    Used as a last-resort fallback when both the dynamic and static
    strike ranges yield zero option contracts. Returns ``None`` if the
    archive has no active options for the underlying at all.
    """
    now = now or _now_ist()
    cutoff_today = now.replace(hour=15, minute=30, second=0, microsecond=0)

    strikes: list[float] = []
    for item in instruments:
        if not _is_active_option(item, target, now, cutoff_today):
            continue
        strike = _strike_of(item)
        if strike is not None:
            strikes.append(strike)

    if not strikes:
        return None

    lo, hi = min(strikes), max(strikes)
    logger.info(
        "Auto-detected strike range %s: [%.0f, %.0f] from %d active option(s)",
        target, lo, hi, len(strikes),
    )
    return lo, hi

# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def filter_nearest_expiry_options(
    instruments: list[dict[str, Any]],
    underlying_symbols: list[str],
    strike_ranges: dict[str, tuple[float, float]],
    instrument_types: tuple[str, ...] = _DEFAULT_TYPES,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return nearest-expiry option contracts (strike + type filtered)."""
    now = now or _now_ist()
    today = now.date()
    cutoff_today = now.replace(hour=15, minute=30, second=0, microsecond=0)

    targets = [s.strip().upper() for s in underlying_symbols if s.strip()]
    result: list[dict[str, Any]] = []

    for target in targets:
        smin, smax = strike_ranges.get(target, (float("-inf"), float("inf")))
        lo, hi = min(smin, smax), max(smin, smax)
        if lo == hi:
            logger.warning(
                "Empty strike range for %s (lo==hi==%s); skipping",
                target, lo,
            )
            continue

        candidates: list[tuple[datetime, dict[str, Any]]] = []
        for item in instruments:
            if not isinstance(item, dict):
                continue
            itype = _norm(item.get("instrument_type"))
            if itype not in instrument_types:
                continue
            if not _matches_underlying(item, target):
                continue

            exp = _parse_expiry(item.get("expiry"))
            if exp is None:
                continue
            if exp.date() < today:
                continue
            if exp.date() == today and now >= cutoff_today:
                continue

            strike = _strike_of(item)
            if strike is None or not (lo <= strike <= hi):
                continue

            candidates.append((exp, item))

        if not candidates:
            logger.warning(
                "No active options found for %s in strike window [%s, %s]",
                target, lo, hi,
            )
            continue

        nearest = min(exp.date() for exp, _ in candidates)
        kept = [item for exp, item in candidates if exp.date() == nearest]

        kept.sort(
            key=lambda x: (
                float(x.get("strike_price") or 0),
                str(x.get("instrument_type") or ""),
            )
        )
        logger.info(
            "Options %s: nearest_expiry=%s kept=%d (strikes %s..%s types=%s)",
            target,
            nearest.isoformat(),
            len(kept),
            lo,
            hi,
            ",".join(instrument_types),
        )
        result.extend(kept)

    return result

# ---------------------------------------------------------------------------
# Index rows
# ---------------------------------------------------------------------------

def _build_index_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key, symbol in (
        (settings.nifty_index_instrument_key, settings.nifty_underlying),
        (settings.sensex_index_instrument_key, settings.sensex_underlying),
    ):
        if not key or not symbol:
            continue
        rows.append(
            {
                "instrument_key": key,
                "underlying_symbol": symbol,
                "instrument_type": "INDEX",
                "trading_symbol": symbol,
            }
        )
    return rows

# ---------------------------------------------------------------------------
# Per-underlying strike-range resolution (3-tier fallback)
# ---------------------------------------------------------------------------

def _resolve_range_with_fallback(
    archive: list[dict[str, Any]],
    target_upper: str,
    target_orig: str,
    dynamic: dict[str, tuple[float, float]],
    static_min: float,
    static_max: float,
) -> list[dict[str, Any]]:
    """Return options for one underlying, using a 3-tier strike fallback.

    Tier 1  dynamic range (from intraday candles)
    Tier 2  static range  (from .env)
    Tier 3  auto-detected range (from the archive itself)
    """
    dynamic_range = dynamic.get(target_upper)

    # ---- Tier 1: dynamic --------------------------------------------
    if dynamic_range is not None:
        options = filter_nearest_expiry_options(
            archive,
            underlying_symbols=[target_orig],
            strike_ranges={target_upper: dynamic_range},
        )
        if options:
            logger.info(
                "Using DYNAMIC strike range for %s: [%s, %s] -> %d option(s)",
                target_upper, dynamic_range[0], dynamic_range[1], len(options),
            )
            return options
        logger.warning(
            "Dynamic strike range %s for %s yielded 0 options; "
            "falling back to static range",
            dynamic_range, target_upper,
        )

    # ---- Tier 2: static ---------------------------------------------
    static_range = (static_min, static_max)
    options = filter_nearest_expiry_options(
        archive,
        underlying_symbols=[target_orig],
        strike_ranges={target_upper: static_range},
    )
    if options:
        logger.info(
            "Using STATIC strike range for %s: [%s, %s] -> %d option(s)",
            target_upper, static_min, static_max, len(options),
        )
        return options
    logger.warning(
        "Static strike range %s for %s yielded 0 options; "
        "falling back to auto-detected range",
        static_range, target_upper,
    )

    # ---- Tier 3: auto-detect from the archive -----------------------
    auto = _auto_detect_strike_range(archive, target_upper)
    if auto is None:
        logger.error(
            "No active options exist for %s in the entire archive; "
            "nothing to include in all_instruments.json",
            target_upper,
        )
        return []

    options = filter_nearest_expiry_options(
        archive,
        underlying_symbols=[target_orig],
        strike_ranges={target_upper: auto},
    )
    logger.info(
        "Using AUTO-DETECTED strike range for %s: [%.0f, %.0f] -> %d option(s)",
        target_upper, auto[0], auto[1], len(options),
    )
    return options

# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def discover_instruments(force_download: bool = False) -> list[dict[str, Any]]:
    """Download the archive and return the full instrument list to fetch.

    Also writes ``all_instruments.json`` keyed by ``instrument_key``.
    """
    archive = download_instruments(force=force_download)

    # ---- Dynamic strike ranges --------------------------------------
    try:
        dynamic = compute_dynamic_strike_ranges()
    except Exception:
        logger.exception(
            "Dynamic strike range computation failed; "
            "falling back to static values",
        )
        dynamic = {}

    # ---- Per-underlying option resolution ---------------------------
    nifty_options = _resolve_range_with_fallback(
        archive,
        target_upper=settings.nifty_underlying.upper(),
        target_orig=settings.nifty_underlying,
        dynamic=dynamic,
        static_min=settings.nifty_strike_min,
        static_max=settings.nifty_strike_max,
    )

    sensex_options = _resolve_range_with_fallback(
        archive,
        target_upper=settings.sensex_underlying.upper(),
        target_orig=settings.sensex_underlying,
        dynamic=dynamic,
        static_min=settings.sensex_strike_min,
        static_max=settings.sensex_strike_max,
    )

    # ---- Index rows --------------------------------------------------
    indices = _build_index_rows()

    combined = indices + nifty_options + sensex_options
    logger.info(
        "Discovered %d instrument(s): %d index rows + %d option contracts "
        "(NIFTY=%d, SENSEX=%d)",
        len(combined),
        len(indices),
        len(nifty_options) + len(sensex_options),
        len(nifty_options),
        len(sensex_options),
    )

    # ---- all_instruments.json snapshot -------------------------------
    try:
        save_all_instruments(combined)
    except Exception:
        logger.exception(
            "all_instruments.json write failed; continuing with backup",
        )

    return combined
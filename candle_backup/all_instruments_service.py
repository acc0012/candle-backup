# candle_backup/all_instruments_service.py
"""Write a snapshot of every discovered instrument to all_instruments.json.

Output shape — one object keyed by ``instrument_key``. Each record
carries a ``relative_path`` field pointing at the instrument's own
directory under ``DATA_DIR`` (e.g.
``"index/nifty/all_instruments.json"`` for an index, or
``"nifty/22550_PE/all_instruments.json"`` for an option).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from candle_backup.config import settings
from candle_backup.instrument_paths import (
    is_index_instrument,
    relative_dir_for,
    relative_path_for,
    resolve_underlying,
)
from candle_backup.logger import get_logger

logger = get_logger(__name__)

_MARKET_TZ = ZoneInfo("Asia/Kolkata")

# ---------------------------------------------------------------------------
# Expiry conversion (unchanged)
# ---------------------------------------------------------------------------

def _readable_expiry(raw: Any) -> str | None:
    if raw is None:
        return None

    try:
        ts = float(raw)
        if ts > 1e11:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=_MARKET_TZ).date().isoformat()
    except (TypeError, ValueError, OSError):
        pass

    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        try:
            return datetime.fromisoformat(
                s.replace("Z", "+00:00")
            ).date().isoformat()
        except ValueError:
            try:
                return datetime.strptime(s[:10], "%Y-%m-%d").date().isoformat()
            except ValueError:
                return None
    return None

# ---------------------------------------------------------------------------
# Per-instrument record (now includes relative_path)
# ---------------------------------------------------------------------------

def _record_for(item: dict[str, Any], snapshot_path: Path) -> dict[str, Any]:
    """Build one record with a ``relative_path`` field.

    The ``relative_path`` points to the instrument's own directory,
    suffixed with ``all_instruments.json`` — i.e. the path a consumer
    would use if each instrument ever got its own small index file.
    For the canonical shared snapshot at ``<DATA_DIR>/all_instruments.json``
    the top-level file is still just ``all_instruments.json``; the
    per-record value lets a UI build, e.g.::

        data/<relative_path>           # instrument dir
        data/<relative_path dir>/candles.json
    """
    # Directory of the instrument (e.g. "nifty/22550_PE") relative
    # to DATA_DIR. Empty for the top-level snapshot file itself.
    dir_rel = relative_dir_for(item) if isinstance(item, dict) else ""
    if dir_rel:
        per_record_rel = f"{dir_rel}/all_instruments.json"
    else:
        # Fallback: strip the DATA_DIR prefix from the shared file.
        per_record_rel = relative_path_for(snapshot_path)

    return {
        "instrument_key": item.get("instrument_key"),
        "relative_path": per_record_rel,
        "underlying": (
            item.get("underlying_symbol")
            or item.get("underlying_key")
            or item.get("name")
            or item.get("asset_symbol")
        ),
        "strike_price": item.get("strike_price"),
        "strike_type": item.get("instrument_type"),
        "expiry": _readable_expiry(item.get("expiry")),
        "trading_symbol": item.get("trading_symbol"),
    }

# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def save_all_instruments(
    instruments: list[dict[str, Any]],
) -> Path | None:
    """Write ``all_instruments.json`` keyed by ``instrument_key``."""
    target = Path(settings.all_instruments_file)

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        logger.exception(
            "Unable to create parent directory for %s", target,
        )
        return None

    # ---- Build payload -----------------------------------------------
    payload: dict[str, dict[str, Any]] = {}
    index_count = 0
    option_count = 0
    for item in instruments:
        if not isinstance(item, dict):
            continue
        key = item.get("instrument_key")
        if not isinstance(key, str) or not key.strip():
            continue
        record = _record_for(item, target)
        payload[key.strip()] = record
        # Prefer the explicit INDEX tag when present; fall back to
        # the shared helper so manual list items still classify.
        is_idx = (
            str(record.get("strike_type") or "").upper() == "INDEX"
            or is_index_instrument(item)
        )
        if is_idx:
            index_count += 1
        else:
            option_count += 1

    # ---- Write atomically --------------------------------------------
    try:
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(target)
    except Exception:
        logger.exception("Failed writing all_instruments.json to %s", target)
        return None

    logger.info(
        "Wrote %d instrument(s) to %s (indices=%d options=%d)",
        len(payload), target, index_count, option_count,
    )

    if index_count > 0 and option_count == 0:
        logger.warning(
            "all_instruments.json contains ONLY index rows (%d) and no "
            "option contracts. This usually means the strike filter "
            "matched nothing — check the [STRIKE-RANGE] log lines and "
            "the static fallback range in .env.",
            index_count,
        )

    # ---- Read-back verification -------------------------------------
    try:
        readback = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(readback, dict):
            logger.error(
                "Verification failed: all_instruments.json is not a JSON "
                "object (got %s)",
                type(readback).__name__,
            )
        elif len(readback) != len(payload):
            logger.error(
                "Verification mismatch: wrote %d entries but read back %d",
                len(payload), len(readback),
            )
        else:
            logger.info(
                "Verified all_instruments.json: %d entries present",
                len(readback),
            )
    except Exception:
        logger.exception("Failed to verify all_instruments.json")

    return target
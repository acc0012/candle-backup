# candle_backup/index_instruments_service.py
"""Write a snapshot of every index row from the Upstox archive.

The snapshot is written to ``settings.index_instruments_file`` — by
default ``<DATA_DIR>/index_instruments.json`` — and therefore is picked
up automatically by the MEGA backup zip (as ``index_instruments.json``
at the archive root).

Output shape
------------

    {
      "updated_at": "2026-10-23T...",
      "relative_path": "index_instruments.json",
      "total": 87,
      "indexs": [
        {
          "instrument_key": "NSE_INDEX|Nifty 50",
          "trading_symbol": "NIFTY 50",
          "name": "NIFTY 50",
          "segment": "NSE_INDEX",
          "exchange": "NSE",
          ... all other metadata fields from the archive ...
        },
        ...
      ]
    }

Index detection
---------------
An item is treated as an index row when any of the following holds:

* ``segment`` ends with ``"_INDEX"`` (``NSE_INDEX``, ``BSE_INDEX``,
  ``MCX_INDEX`` …).
* ``instrument_key`` begins with a ``*_INDEX|`` prefix.
* ``instrument_type`` equals ``"INDEX"``.
* The shared ``instrument_paths.is_index_instrument`` helper returns
  ``True`` (covers the hand-built rows used by the rest of the tool).
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
    relative_path_for,
)
from candle_backup.logger import get_logger

logger = get_logger(__name__)

_MARKET_TZ = ZoneInfo("Asia/Kolkata")

_INDEX_SUFFIX = "_INDEX"
_INDEX_TYPE = "INDEX"


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def _is_index_item(item: dict[str, Any]) -> bool:
    """Return True if ``item`` looks like an index row from the archive."""
    if not isinstance(item, dict):
        return False

    segment = str(item.get("segment") or "").strip().upper()
    if segment.endswith(_INDEX_SUFFIX):
        return True

    instrument_key = str(item.get("instrument_key") or "")
    if "|" in instrument_key:
        prefix = instrument_key.split("|", 1)[0].strip().upper()
        if prefix.endswith(_INDEX_SUFFIX):
            return True

    if str(item.get("instrument_type") or "").strip().upper() == _INDEX_TYPE:
        return True

    # Last-resort fallback: the shared helper used by the rest of the tool.
    try:
        return is_index_instrument(item)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Record building
# ---------------------------------------------------------------------------

def _record_for(item: dict[str, Any]) -> dict[str, Any]:
    """Return one clean index record.

    ``instrument_key`` and ``trading_symbol`` are always present (may be
    ``None`` only if the upstream archive omitted them). Every other
    non-empty field from the archive is copied verbatim so downstream
    consumers get the full upstream metadata without us having to
    maintain a hard-coded field list.
    """
    record: dict[str, Any] = {
        "instrument_key": item.get("instrument_key"),
        "trading_symbol": item.get("trading_symbol"),
    }

    for key, value in item.items():
        if key in record:
            continue
        if value in (None, "", [], {}):
            continue
        record[key] = value

    return record


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def save_index_instruments(
    instruments: list[dict[str, Any]],
) -> Path | None:
    """Write ``index_instruments.json`` with every index row.

    Returns the target path on success, or ``None`` on any failure.
    Never raises — a write failure must not abort the backup run.
    """
    target = Path(settings.index_instruments_file)

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        logger.exception(
            "Unable to create parent directory for %s", target,
        )
        return None

    # ---- Collect + de-duplicate index records ------------------------
    records: list[dict[str, Any]] = []
    seen: set[str] = set()

    for item in instruments:
        if not _is_index_item(item):
            continue
        key = item.get("instrument_key")
        if not isinstance(key, str) or not key.strip():
            continue
        key = key.strip()
        if key in seen:
            continue
        seen.add(key)
        records.append(_record_for(item))

    records.sort(key=lambda r: str(r.get("instrument_key") or ""))

    # ---- Compose + write the payload atomically ----------------------
    payload: dict[str, Any] = {
        "updated_at": datetime.now(_MARKET_TZ).isoformat(),
        "relative_path": relative_path_for(target),
        "total": len(records),
        "indexs": records,
    }

    try:
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(target)
    except Exception:
        logger.exception(
            "Failed writing index_instruments.json to %s", target,
        )
        return None

    logger.info(
        "Wrote %d index instrument(s) to %s", len(records), target,
    )

    if not records:
        logger.warning(
            "index_instruments.json was written but contains 0 index "
            "rows. This usually means the upstream archive layout "
            "changed — check INSTRUMENTS_URL and the segment field.",
        )

    # ---- Read-back verification -------------------------------------
    try:
        readback = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(readback, dict):
            logger.error(
                "Verification failed: index_instruments.json is not a "
                "JSON object (got %s)", type(readback).__name__,
            )
        elif readback.get("total") != len(records):
            logger.error(
                "Verification mismatch: wrote %d entries but read back "
                "total=%s", len(records), readback.get("total"),
            )
        elif len(readback.get("indexs") or []) != len(records):
            logger.error(
                "Verification mismatch: wrote %d entries but read back "
                "%d list entries",
                len(records), len(readback.get("indexs") or []),
            )
        else:
            logger.info(
                "Verified index_instruments.json: %d entries present",
                len(records),
            )
    except Exception:
        logger.exception("Failed to verify index_instruments.json")

    return target
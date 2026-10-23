# candle_backup/all_instruments_service.py
"""Write a snapshot of every discovered instrument to all_instruments.json.

Output shape — one object keyed by ``instrument_key``::

    {
      "BSE_FO|866927": {
        "instrument_key": "BSE_FO|866927",
        "underlying": "SENSEX",
        "strike_price": 72100.0,
        "strike_type": "CE",
        "expiry": "2026-10-29",
        "trading_symbol": "SENSEX 72100 CE 29 OCT 26"
      },
      "NSE_INDEX|Nifty 50": { ... },
      ...
    }

Key notes
---------
* ``expiry`` is stored as a **readable ISO date** (``"YYYY-MM-DD"``),
  converted from the raw epoch-ms value in the Upstox archive.
* Index rows (``NSE_INDEX|…`` / ``BSE_INDEX|…``) carry ``strike_price:
  null`` and ``strike_type: "INDEX"``.
* The file is written atomically (``.tmp`` + ``replace``).
* After the write, the file is **read back** and the entry count is
  compared against what we intended to write — any mismatch is logged
  as an error so silent corruption cannot go unnoticed.
* Explicit counts of index vs option rows are logged. If the write
  contains index rows but zero options, a loud warning is emitted
  because that almost always means the strike filter matched nothing.
* Failures are logged and swallowed — never abort the backup.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from candle_backup.config import settings
from candle_backup.logger import get_logger

logger = get_logger(__name__)

_MARKET_TZ = ZoneInfo("Asia/Kolkata")


# ---------------------------------------------------------------------------
# Expiry conversion
# ---------------------------------------------------------------------------

def _readable_expiry(raw: Any) -> str | None:
    """Return ``"YYYY-MM-DD"`` from epoch-ms, epoch-s, or ISO string."""
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
# Per-instrument record
# ---------------------------------------------------------------------------

def _record_for(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "instrument_key": item.get("instrument_key"),
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
    """Write ``all_instruments.json`` keyed by ``instrument_key``.

    Best-effort: any failure (IO, encoding) is logged and ``None`` is
    returned so callers can continue.
    """
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
        record = _record_for(item)
        payload[key.strip()] = record
        if str(record.get("strike_type") or "").upper() == "INDEX":
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

    # ---- Loud warning when only indices landed ----------------------
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
# candle_backup/sr_levels.py
"""Support / Resistance pivot detection and persistence.

Mirrors the Pine Script indicator "Support and Resistance Levels with
Breaks" (LuxAlgo, CC BY-NC-SA 4.0):

* A **resistance** pivot is a bar whose ``high`` is strictly greater
  than the ``high`` of every bar in the ``left_bars`` bars before it
  AND every bar in the ``right_bars`` bars after it.
* A **support** pivot is the same on ``low``.
* A pivot is only **confirmed** ``right_bars`` bars after the pivot bar
  itself — this is exactly the moment ``ta.pivothigh`` /
  ``ta.pivotlow`` return a non-``na`` value in Pine Script.

Every newly confirmed pivot is appended to ``sr_levels.json``. The
file also carries a top-level ``relative_path`` field describing its
own location relative to ``DATA_DIR`` (e.g.
``"nifty/22550_PE/sr_levels.json"``).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from candle_backup.instrument_paths import relative_path_for
from candle_backup.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Pivot detection
# ---------------------------------------------------------------------------

def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

def _candle_high(candle: dict[str, Any]) -> float | None:
    return _to_float(candle.get("high"))

def _candle_low(candle: dict[str, Any]) -> float | None:
    return _to_float(candle.get("low"))

def find_pivots(
    candles: list[dict[str, Any]],
    left_bars: int,
    right_bars: int,
) -> list[dict[str, Any]]:
    """Return every confirmed pivot in ``candles``.

    Each record has::

        {
          "level": "resistance" | "support",
          "price": float,
          "pivot_bar_index": int,
          "pivot_time": str,
          "confirmed_bar_index": int,
          "confirmed_time": str,
        }

    The function is O(n * (left_bars + right_bars)) — acceptable for the
    amount of 1-minute candles kept on disk (a few thousand per
    instrument).
    """
    if left_bars <= 0 or right_bars <= 0:
        raise ValueError(
            f"left_bars and right_bars must be > 0 "
            f"(got left={left_bars} right={right_bars})"
        )

    n = len(candles)
    if n < left_bars + right_bars + 1:
        return []

    pivots: list[dict[str, Any]] = []

    for pivot_idx in range(left_bars, n - right_bars):
        pivot_bar = candles[pivot_idx]
        pivot_time = pivot_bar.get("timestamp")
        high = _candle_high(pivot_bar)
        low = _candle_low(pivot_bar)

        # ---- Resistance check -------------------------------------
        if high is not None:
            is_res = True
            for j in range(pivot_idx - left_bars, pivot_idx):
                v = _candle_high(candles[j])
                if v is None or high <= v:
                    is_res = False
                    break
            if is_res:
                for j in range(pivot_idx + 1, pivot_idx + right_bars + 1):
                    v = _candle_high(candles[j])
                    if v is None or high <= v:
                        is_res = False
                        break
            if is_res:
                conf_idx = pivot_idx + right_bars
                pivots.append(
                    {
                        "level": "resistance",
                        "price": round(high, 4),
                        "pivot_bar_index": pivot_idx,
                        "pivot_time": pivot_time,
                        "confirmed_bar_index": conf_idx,
                        "confirmed_time": candles[conf_idx].get("timestamp"),
                    }
                )

        # ---- Support check ----------------------------------------
        if low is not None:
            is_sup = True
            for j in range(pivot_idx - left_bars, pivot_idx):
                v = _candle_low(candles[j])
                if v is None or low >= v:
                    is_sup = False
                    break
            if is_sup:
                for j in range(pivot_idx + 1, pivot_idx + right_bars + 1):
                    v = _candle_low(candles[j])
                    if v is None or low >= v:
                        is_sup = False
                        break
            if is_sup:
                conf_idx = pivot_idx + right_bars
                pivots.append(
                    {
                        "level": "support",
                        "price": round(low, 4),
                        "pivot_bar_index": pivot_idx,
                        "pivot_time": pivot_time,
                        "confirmed_bar_index": conf_idx,
                        "confirmed_time": candles[conf_idx].get("timestamp"),
                    }
                )

    return pivots

# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _normalize_time_for_sort(value: Any) -> str:
    if value is None:
        return ""
    return str(value)

def _record_key(rec: dict[str, Any]) -> tuple[str, int | None]:
    level = str(rec.get("level") or "").lower()
    idx = rec.get("pivot_bar_index")
    try:
        idx_int = int(idx) if idx is not None else None
    except (TypeError, ValueError):
        idx_int = None
    return level, idx_int

def load_existing(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Unable to read existing sr_levels file: %s", path)
        return []

    levels = (
        payload.get("levels") if isinstance(payload, dict) else payload
    )
    if not isinstance(levels, list):
        return []
    return [r for r in levels if isinstance(r, dict)]

def merge_levels(
    existing: list[dict[str, Any]],
    new: list[dict[str, Any]],
    left_bars: int,
    right_bars: int,
) -> list[dict[str, Any]]:
    merged: dict[tuple[str, int | None], dict[str, Any]] = {}

    def _normalize(rec: dict[str, Any]) -> dict[str, Any]:
        out = dict(rec)
        out.setdefault("left_bars", left_bars)
        out.setdefault("right_bars", right_bars)
        return out

    for rec in existing:
        merged[_record_key(rec)] = _normalize(rec)
    for rec in new:
        key = _record_key(rec)
        if key in merged:
            continue
        merged[key] = _normalize(rec)

    return sorted(
        merged.values(),
        key=lambda r: (
            _normalize_time_for_sort(r.get("confirmed_time")),
            str(r.get("level") or ""),
            int(r.get("pivot_bar_index") or 0),
        ),
    )

def save_levels(
    path: Path,
    levels: list[dict[str, Any]],
    *,
    left_bars: int,
    right_bars: int,
) -> None:
    """Atomically write ``sr_levels.json`` with a ``relative_path`` field."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "relative_path": relative_path_for(path),
        "left_bars": left_bars,
        "right_bars": right_bars,
        "count": len(levels),
        "levels": levels,
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp.replace(path)

# ---------------------------------------------------------------------------
# Public entry point (unchanged)
# ---------------------------------------------------------------------------

def update_sr_levels_for_instrument(
    instrument_key: str,
    candles: list[dict[str, Any]],
    path: Path,
    *,
    left_bars: int,
    right_bars: int,
) -> dict[str, Any]:
    """Detect pivots, merge with existing file, persist.

    Returns a small summary dict::

        {"written": bool, "path": str, "total": int,
         "new": int, "existing": int, "error": str | None}
    """
    summary: dict[str, Any] = {
        "written": False,
        "path": str(path),
        "total": 0,
        "new": 0,
        "existing": 0,
        "error": None,
    }

    if not candles:
        return summary

    try:
        new_pivots = find_pivots(candles, left_bars, right_bars)
    except Exception as exc:
        summary["error"] = f"pivot-detection failed: {exc}"
        logger.exception(
            "Pivot detection failed; instrument=%s", instrument_key
        )
        return summary

    existing = load_existing(path)
    merged = merge_levels(existing, new_pivots, left_bars, right_bars)

    summary["existing"] = len(existing)
    summary["new"] = max(0, len(merged) - len(existing))
    summary["total"] = len(merged)

    try:
        save_levels(
            path, merged, left_bars=left_bars, right_bars=right_bars
        )
        summary["written"] = True
    except Exception as exc:
        summary["error"] = f"write failed: {exc}"
        logger.exception(
            "sr_levels write failed; instrument=%s path=%s",
            instrument_key, path,
        )

    return summary
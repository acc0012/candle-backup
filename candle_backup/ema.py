"""Exponential moving averages, ATR, and EMA cross detection + scoring.

Every cross is written to ``ema_crosses.json`` with a compact, stable
shape:

    {
      "timestamp": "2026-10-14T10:45:00+05:30",
      "type": "bullish",
      "fast_ema": 1612.079,
      "slow_ema": 1612.0157,
      "volume": 182340,
      "volume_diff": 0.0,
      "ema_diff": 0.0633,
      "score": 4,
      "max_score": 6,
      "valid": true,
      "filters": {
        "trend": true,
        "separation": false,
        "slope": true,
        "volume": true,
        "body": true,
        "distance": true
      }
    }

``volume_diff`` is ``current volume - average volume`` over
``volume_lookback`` bars. When either side is missing (e.g. an index
chart with no volume feed) it is reported as ``0.0`` so downstream
consumers can rely on a numeric field.

The per-filter thresholds live in ``EmaValidationConfig`` and are
mirrored from the Pine Script indicator you already run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# ---------------------------------------------------------------------------
# Validation config
# ---------------------------------------------------------------------------

@dataclass
class EmaValidationConfig:
    """Thresholds + toggles controlling how a cross is scored."""

    enable_validation: bool = True
    min_validation_score: int = 3

    use_trend_filter: bool = True
    use_separation_filter: bool = True
    use_slope_filter: bool = True
    use_volume_filter: bool = True
    use_body_filter: bool = True
    use_distance_filter: bool = True

    # Filter 1 — trend proxy using slower EMAs (single-series HTF stand-in)
    trend_fast_period: int = 50
    trend_slow_period: int = 100

    # Filter 2 — EMA separation vs ATR
    atr_period: int = 14
    sep_atr_min: float = 0.15

    # Filter 3 — slope lookback
    slope_lookback: int = 3

    # Filter 4 — volume
    volume_lookback: int = 20
    volume_mult: float = 1.0

    # Filter 6 — distance from slow EMA vs ATR
    distance_atr_max: float = 1.5


# ---------------------------------------------------------------------------
# Primitive indicators
# ---------------------------------------------------------------------------

def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def compute_ema(values: list[float | None], period: int) -> list[float | None]:
    """EMA series; ``None`` until ``period`` samples are available.

    The first EMA value is seeded with the SMA of the first ``period``
    samples, then standard EMA smoothing is applied.
    """
    n = len(values)
    out: list[float | None] = [None] * n
    if period <= 0 or n < period:
        return out

    seed_slice = values[:period]
    if any(v is None for v in seed_slice):
        first_valid = next(
            (i for i, v in enumerate(values) if v is not None), None
        )
        if first_valid is None or (n - first_valid) < period:
            return out
        seed_slice = values[first_valid:first_valid + period]
        seed_idx = first_valid + period - 1
    else:
        seed_idx = period - 1

    seed = sum(v for v in seed_slice if v is not None) / period
    out[seed_idx] = seed

    k = 2.0 / (period + 1.0)
    prev = seed
    for i in range(seed_idx + 1, n):
        v = values[i]
        if v is None:
            out[i] = prev
            continue
        prev = v * k + prev * (1.0 - k)
        out[i] = prev
    return out


def compute_sma(values: list[float | None], period: int) -> list[float | None]:
    """Simple moving average; ``None`` until ``period`` samples are available."""
    n = len(values)
    out: list[float | None] = [None] * n
    if period <= 0 or n < period:
        return out

    window: list[float] = []
    for i, v in enumerate(values):
        if v is None:
            window = []
            continue
        window.append(v)
        if len(window) > period:
            window.pop(0)
        if len(window) == period:
            out[i] = sum(window) / period
    return out


def compute_atr(
    candles: list[dict[str, Any]], period: int = 14
) -> list[float | None]:
    """Wilder's ATR over the given candles.

    Returns a list parallel to ``candles``; ``None`` until enough data.
    """
    n = len(candles)
    out: list[float | None] = [None] * n
    if period <= 0 or n < period:
        return out

    trs: list[float | None] = [None] * n
    prev_close: float | None = None
    for i, c in enumerate(candles):
        h = _to_float(c.get("high"))
        l = _to_float(c.get("low"))
        if h is None or l is None:
            prev_close = _to_float(c.get("close")) or prev_close
            continue
        if prev_close is None:
            trs[i] = h - l
        else:
            trs[i] = max(h - l, abs(h - prev_close), abs(l - prev_close))
        prev_close = _to_float(c.get("close")) or prev_close

    first_valid = next((i for i, t in enumerate(trs) if t is not None), None)
    if first_valid is None or (n - first_valid) < period:
        return out

    seed_slice = [trs[first_valid + j] for j in range(period)]
    if any(t is None for t in seed_slice):
        return out

    seed_idx = first_valid + period - 1
    prev_atr = sum(t for t in seed_slice if t is not None) / period
    out[seed_idx] = prev_atr

    for i in range(seed_idx + 1, n):
        t = trs[i]
        if t is None:
            out[i] = prev_atr
            continue
        prev_atr = (prev_atr * (period - 1) + t) / period
        out[i] = prev_atr
    return out


# ---------------------------------------------------------------------------
# Cross detection
# ---------------------------------------------------------------------------

def detect_crosses(
    candles: list[dict[str, Any]],
    fast_period: int = 9,
    slow_period: int = 21,
    validation: EmaValidationConfig | None = None,
) -> list[dict[str, Any]]:
    """Return the list of EMA crosses over ``candles`` with rich fields.

    A cross is recorded when the fast EMA transitions from <= slow to
    > slow (bullish) or from >= slow to < slow (bearish). Every cross
    carries volume, volume_diff, ema_diff and per-filter scoring.
    """
    if fast_period <= 0 or slow_period <= 0 or fast_period >= slow_period:
        raise ValueError(
            f"Invalid EMA periods: fast={fast_period} slow={slow_period}"
        )

    cfg = validation or EmaValidationConfig()

    closes: list[float | None] = [_to_float(c.get("close")) for c in candles]
    volumes: list[float | None] = [_to_float(c.get("volume")) for c in candles]

    first_valid = next((i for i, v in enumerate(closes) if v is not None), None)
    if first_valid is None:
        return []

    valid_closes = closes[first_valid:]

    fast = compute_ema(valid_closes, fast_period)
    slow = compute_ema(valid_closes, slow_period)

    # ---- Optional series used by the validation filters -----------------
    trend_fast: list[float | None] | None = None
    trend_slow: list[float | None] | None = None
    if cfg.use_trend_filter:
        trend_fast = compute_ema(valid_closes, cfg.trend_fast_period)
        trend_slow = compute_ema(valid_closes, cfg.trend_slow_period)

    atrs: list[float | None] | None = None
    if cfg.use_separation_filter or cfg.use_distance_filter:
        atrs = compute_atr(candles[first_valid:], cfg.atr_period)

    avg_vols: list[float | None] | None = None
    if cfg.use_volume_filter:
        avg_vols = compute_sma(volumes[first_valid:], cfg.volume_lookback)

    crosses: list[dict[str, Any]] = []
    prev_diff: float | None = None

    for i, (f, s) in enumerate(zip(fast, slow)):
        if f is None or s is None:
            continue
        diff = f - s
        if prev_diff is not None:
            if prev_diff <= 0 < diff:
                crosses.append(
                    _build_cross_record(
                        candle=candles[first_valid + i],
                        kind="bullish",
                        fast=f,
                        slow=s,
                        i=i,
                        fast_arr=fast,
                        slow_arr=slow,
                        trend_fast_arr=trend_fast,
                        trend_slow_arr=trend_slow,
                        atr_arr=atrs,
                        avgvol_arr=avg_vols,
                        cfg=cfg,
                    )
                )
            elif prev_diff >= 0 > diff:
                crosses.append(
                    _build_cross_record(
                        candle=candles[first_valid + i],
                        kind="bearish",
                        fast=f,
                        slow=s,
                        i=i,
                        fast_arr=fast,
                        slow_arr=slow,
                        trend_fast_arr=trend_fast,
                        trend_slow_arr=trend_slow,
                        atr_arr=atrs,
                        avgvol_arr=avg_vols,
                        cfg=cfg,
                    )
                )
        prev_diff = diff

    return crosses


# ---------------------------------------------------------------------------
# Per-cross record + scoring
# ---------------------------------------------------------------------------

def _build_cross_record(
    *,
    candle: dict[str, Any],
    kind: str,
    fast: float,
    slow: float,
    i: int,
    fast_arr: list[float | None],
    slow_arr: list[float | None],
    trend_fast_arr: list[float | None] | None,
    trend_slow_arr: list[float | None] | None,
    atr_arr: list[float | None] | None,
    avgvol_arr: list[float | None] | None,
    cfg: EmaValidationConfig,
) -> dict[str, Any]:
    close = _to_float(candle.get("close"))
    open_ = _to_float(candle.get("open"))
    volume = _to_float(candle.get("volume"))

    ema_diff = fast - slow

    atr_val = atr_arr[i] if atr_arr is not None and i < len(atr_arr) else None
    avg_vol = (
        avgvol_arr[i]
        if avgvol_arr is not None and i < len(avgvol_arr)
        else None
    )

    # ---- Derived metrics used by the filters ---------------------------
    sep_ratio: float | None = None
    if atr_val and atr_val > 0:
        sep_ratio = abs(ema_diff) / atr_val

    fast_slope: float | None = None
    slow_slope: float | None = None
    if i >= cfg.slope_lookback:
        f_prev = fast_arr[i - cfg.slope_lookback]
        s_prev = slow_arr[i - cfg.slope_lookback]
        if f_prev is not None:
            fast_slope = fast - f_prev
        if s_prev is not None:
            slow_slope = slow - s_prev

    vol_ratio: float | None = None
    if avg_vol and avg_vol > 0 and volume is not None:
        vol_ratio = volume / avg_vol

    distance: float | None = None
    if close is not None:
        distance = abs(close - slow)

    dist_ratio: float | None = None
    if distance is not None and atr_val and atr_val > 0:
        dist_ratio = distance / atr_val

    # ---- Evaluate filters ----------------------------------------------
    filters: dict[str, bool] = {}

    if cfg.use_trend_filter:
        t_f = (
            trend_fast_arr[i]
            if trend_fast_arr is not None and i < len(trend_fast_arr)
            else None
        )
        t_s = (
            trend_slow_arr[i]
            if trend_slow_arr is not None and i < len(trend_slow_arr)
            else None
        )
        if t_f is not None and t_s is not None:
            filters["trend"] = (t_f > t_s) if kind == "bullish" else (t_f < t_s)
        else:
            filters["trend"] = False

    if cfg.use_separation_filter:
        filters["separation"] = bool(
            sep_ratio is not None and sep_ratio >= cfg.sep_atr_min
        )

    if cfg.use_slope_filter:
        if fast_slope is not None and slow_slope is not None:
            if kind == "bullish":
                filters["slope"] = fast_slope > 0 and slow_slope > 0
            else:
                filters["slope"] = fast_slope < 0 and slow_slope < 0
        else:
            filters["slope"] = False

    if cfg.use_volume_filter:
        # If the chart has no volume, don't penalize the cross.
        if volume is None or volume == 0:
            filters["volume"] = True
        elif vol_ratio is not None:
            filters["volume"] = vol_ratio >= cfg.volume_mult
        else:
            filters["volume"] = False

    if cfg.use_body_filter:
        if close is not None and open_ is not None:
            if kind == "bullish":
                filters["body"] = close > slow and close > open_
            else:
                filters["body"] = close < slow and close < open_
        else:
            filters["body"] = False

    if cfg.use_distance_filter:
        # No ATR → treat as neutral pass (mirrors Pine Script behaviour).
        if dist_ratio is None:
            filters["distance"] = True
        else:
            filters["distance"] = dist_ratio <= cfg.distance_atr_max

    score = sum(1 for v in filters.values() if v)
    max_score = len(filters)
    effective_threshold = min(cfg.min_validation_score, max_score)
    valid = (not cfg.enable_validation) or (score >= effective_threshold)

    # ---- volume_diff = current volume - average volume ------------------
    volume_diff: float | None = None
    if volume is not None and avg_vol is not None:
        volume_diff = volume - avg_vol

    # ---- Slim public record --------------------------------------------
    return {
        "timestamp": candle.get("timestamp"),
        "type": kind,
        "fast_ema": round(fast, 4),
        "slow_ema": round(slow, 4),
        "volume": volume,
        "volume_diff": (
            round(volume_diff, 4) if volume_diff is not None else 0.0
        ),
        "ema_diff": round(ema_diff, 4),
        "score": score,
        "max_score": max_score,
        "valid": valid,
        "filters": filters,
    }
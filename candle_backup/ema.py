"""Exponential moving averages and cross detection."""

from __future__ import annotations

from typing import Any


def compute_ema(values: list[float], period: int) -> list[float | None]:
    """EMA series; ``None`` until ``period`` samples are available.

    The first EMA value is seeded with the SMA of the first ``period``
    samples, then standard EMA smoothing is applied.
    """
    n = len(values)
    out: list[float | None] = [None] * n
    if period <= 0 or n < period:
        return out

    seed = sum(values[:period]) / period
    out[period - 1] = seed

    k = 2.0 / (period + 1.0)
    prev = seed
    for i in range(period, n):
        prev = values[i] * k + prev * (1.0 - k)
        out[i] = prev
    return out


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def detect_crosses(
    candles: list[dict[str, Any]],
    fast_period: int = 9,
    slow_period: int = 21,
) -> list[dict[str, Any]]:
    """Return the list of EMA crosses over ``candles``.

    A cross is recorded when the fast EMA transitions from <= slow to
    > slow (bullish) or from >= slow to < slow (bearish).
    """
    if fast_period <= 0 or slow_period <= 0 or fast_period >= slow_period:
        raise ValueError(
            f"Invalid EMA periods: fast={fast_period} slow={slow_period}"
        )

    closes: list[float] = []
    for c in candles:
        v = _to_float(c.get("close"))
        closes.append(v if v is not None else float("nan"))

    # Skip leading NaNs so EMA seeds correctly on real data.
    first_valid = next(
        (i for i, v in enumerate(closes) if v == v),  # v == v filters NaN
        None,
    )
    if first_valid is None:
        return []

    valid_closes = closes[first_valid:]
    fast = compute_ema(valid_closes, fast_period)
    slow = compute_ema(valid_closes, slow_period)

    crosses: list[dict[str, Any]] = []
    prev_diff: float | None = None
    for i, (f, s) in enumerate(zip(fast, slow)):
        if f is None or s is None:
            continue
        diff = f - s
        if prev_diff is not None:
            if prev_diff <= 0 < diff:
                crosses.append(
                    _cross_record(
                        candles[first_valid + i], "bullish", f, s
                    )
                )
            elif prev_diff >= 0 > diff:
                crosses.append(
                    _cross_record(
                        candles[first_valid + i], "bearish", f, s
                    )
                )
        prev_diff = diff
    return crosses


def _cross_record(
    candle: dict[str, Any],
    kind: str,
    fast: float,
    slow: float,
) -> dict[str, Any]:
    close = _to_float(candle.get("close"))
    return {
        "timestamp": candle.get("timestamp"),
        "type": kind,
        "fast_ema": round(fast, 4),
        "slow_ema": round(slow, 4),
        "close": close,
    }
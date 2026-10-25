# candle_backup/instrument_paths.py
"""Path and underlying resolution for the candle cache.

Layout
------
Options and futures::

    data/<underlying>/<strike>_<strike_type>/candles.json
    data/<underlying>/<strike>_<strike_type>/ema_crosses.json
    data/<underlying>/<strike>_<strike_type>/sr_levels.json

Index / underlying rows::

    data/index/<underlying>/candles.json
    data/index/<underlying>/ema_crosses.json
    data/index/<underlying>/sr_levels.json

Non-option, non-index instruments (fallback)::

    data/<underlying>/<instrument_type or 'spot'>/candles.json

Every generated JSON file carries a ``relative_path`` field (POSIX
style, relative to ``settings.data_dir``) so downstream consumers can
build a path/link without re-deriving the layout. See
``relative_path_for``.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from candle_backup.config import settings

_INDEX_ALIASES: dict[str, str] = {
    "nifty50": "nifty",
    "nifty": "nifty",
    "banknifty": "banknifty",
    "finnifty": "finnifty",
    "midcpnifty": "midcpnifty",
    "sensex": "sensex",
    "bankex": "bankex",
}

def normalize_underlying_name(name: str) -> str:
    if not isinstance(name, str):
        return "unknown"
    raw = name.strip().lower().replace(" ", "").replace("_", "")
    if not raw:
        return "unknown"
    return _INDEX_ALIASES.get(raw, raw)

def _strip_index_prefix(value: str) -> str:
    if "|" in value:
        return value.split("|", 1)[1]
    return value

def is_index_instrument(item: dict[str, Any]) -> bool:
    ikey = str(item.get("instrument_key") or "")
    if ikey.startswith("NSE_INDEX|") or ikey.startswith("BSE_INDEX|"):
        return True

    strike = item.get("strike_price")
    has_strike = strike not in (None, "", 0) and strike != 0
    itype = item.get("instrument_type")
    source = str(item.get("source") or "")
    if not has_strike and not itype and source == "startup_underlying":
        return True

    return False

def resolve_underlying(item: dict[str, Any]) -> str:
    for key in ("underlying", "underlying_symbol", "asset", "root_symbol"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return normalize_underlying_name(value)

    uk = item.get("underlying_key")
    if isinstance(uk, str) and uk.strip():
        return normalize_underlying_name(_strip_index_prefix(uk))

    symbol = str(item.get("trading_symbol") or "").upper()
    for name in ("MIDCPNIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "NIFTY"):
        if symbol.startswith(name):
            return normalize_underlying_name(name)

    if is_index_instrument(item):
        ikey = str(item.get("instrument_key") or "")
        return normalize_underlying_name(_strip_index_prefix(ikey))

    return "unknown"

def _instrument_dir(item: dict[str, Any]) -> Path:
    underlying = resolve_underlying(item)

    if is_index_instrument(item):
        return settings.data_dir / "index" / underlying

    strike = item.get("strike_price")
    strike_type = str(item.get("instrument_type") or "").strip().upper()

    try:
        strike_num = float(strike)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        strike_num = 0.0

    if strike_num > 0:
        strike_str = (
            str(int(strike_num)) if strike_num.is_integer() else str(strike_num)
        )
        folder = f"{strike_str}_{strike_type}" if strike_type else strike_str
    else:
        folder = strike_type or "spot"

    return settings.data_dir / underlying / folder

def build_candle_path(item: dict[str, Any]) -> Path:
    """Path to the merged candles file (historical + intraday)."""
    return _instrument_dir(item) / "candles.json"

def build_ema_path(item: dict[str, Any]) -> Path:
    """Path to the EMA crosses file."""
    return _instrument_dir(item) / "ema_crosses.json"

def build_sr_path(item: dict[str, Any]) -> Path:
    """Path to the support/resistance levels file."""
    return _instrument_dir(item) / "sr_levels.json"

# ---------------------------------------------------------------------------
# relative_path helpers
# ---------------------------------------------------------------------------

def relative_path_for(path: Path) -> str:
    """Return ``path`` as a POSIX-style string relative to ``DATA_DIR``.

    Falls back to the absolute path (POSIX style) if ``path`` is not
    under ``DATA_DIR``. Never returns backslashes, so the value is safe
    to embed in JSON and to use in URLs.
    """
    p = Path(path)
    try:
        rel = p.resolve().relative_to(settings.data_dir.resolve())
        return rel.as_posix()
    except Exception:
        return p.as_posix()

def relative_dir_for(item: dict[str, Any]) -> str:
    """Return the instrument directory as a POSIX path relative to DATA_DIR.

    Example: ``"index/nifty"`` or ``"nifty/22550_PE"``.
    """
    return relative_path_for(_instrument_dir(item))

_write_locks: dict[str, threading.Lock] = {}
_write_locks_guard = threading.Lock()

def get_file_write_lock(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _write_locks_guard:
        lock = _write_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _write_locks[key] = lock
        return lock
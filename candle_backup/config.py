# candle_backup/config.py
"""Configuration loaded from .env or the process environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parents[1]
ENV_FILE = BASE_DIR / ".env"

load_dotenv(dotenv_path=ENV_FILE, override=False)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_str(name: str, default: str = "") -> str:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().strip('"').strip("'")


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _env_path(name: str, default: Path) -> Path:
    """Path env var: blank -> default; absolute -> as-is; relative ->
    resolved against ``BASE_DIR`` so it is stable regardless of CWD."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().strip('"').strip("'")
    p = Path(value)
    if not p.is_absolute():
        p = (BASE_DIR / p).resolve()
    return p


@dataclass(frozen=True, slots=True)
class Settings:
    # ---- HTTP ---------------------------------------------------------
    request_timeout_seconds: float = float(
        os.getenv("REQUEST_TIMEOUT_SECONDS", "30")
    )

    # ---- Logging ------------------------------------------------------
    log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()
    log_file: Path = BASE_DIR / os.getenv("LOG_FILE", "logs/backup.log")

    # ---- Candle cache -------------------------------------------------
    data_dir: Path = BASE_DIR / os.getenv("DATA_DIR", "data")
    history_lookback_days: int = int(os.getenv("HISTORY_LOOKBACK_DAYS", "10"))
    history_max_concurrency: int = int(
        os.getenv("HISTORY_MAX_CONCURRENCY", "8")
    )

    history_strict_completeness: bool = _env_bool(
        "HISTORY_STRICT_COMPLETENESS", True
    )
    history_verify_overlap_days: int = int(
        os.getenv("HISTORY_VERIFY_OVERLAP_DAYS", "1")
    )

    # ---- Market window ------------------------------------------------
    market_timezone: str = os.getenv("MARKET_TIMEZONE", "Asia/Kolkata")
    market_open_time: str = os.getenv("MARKET_OPEN_TIME", "09:15")
    market_close_time: str = os.getenv("MARKET_CLOSE_TIME", "15:40")
    intraday_test_mode: bool = _env_bool("INTRADAY_TEST_MODE", True)

    # ---- Instrument archive (free, no auth) ---------------------------
    instruments_url: str = _env_str(
        "INSTRUMENTS_URL",
        "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz",
    )
    instruments_cache_dir: Path = BASE_DIR / os.getenv(
        "INSTRUMENTS_CACHE_DIR", "data/_cache"
    )

    # ---- All-instruments snapshot file --------------------------------
    # Written after every discover_instruments() run, keyed by
    # instrument_key with expiry already converted to a readable date.
    #
    # Default: <DATA_DIR>/all_instruments.json
    #   -> "data/all_instruments.json" with the default DATA_DIR=data.
    # Because it lives inside data/, it is automatically included in
    # the MEGA backup zip and shows up as "all_instruments.json" at
    # the archive root.
    #
    # Override via ALL_INSTRUMENTS_FILE (absolute or BASE_DIR-relative).
    all_instruments_file: Path = _env_path(
        "ALL_INSTRUMENTS_FILE",
        BASE_DIR / os.getenv("DATA_DIR", "data") / "all_instruments.json",
    )

    # ---- Underlyings to back up (option chains) -----------------------
    nifty_underlying: str = _env_str("NIFTY_UNDERLYING", "NIFTY")
    sensex_underlying: str = _env_str("SENSEX_UNDERLYING", "SENSEX")

    # Main index instrument keys — fetched intraday to derive the
    # dynamic strike window, and backed up under data/index/<name>/.
    nifty_index_instrument_key: str = _env_str(
        "NIFTY_INDEX_INSTRUMENT_KEY", "NSE_INDEX|Nifty 50"
    )
    sensex_index_instrument_key: str = _env_str(
        "SENSEX_INDEX_INSTRUMENT_KEY", "BSE_INDEX|SENSEX"
    )

    # ---- Dynamic strike ranges ---------------------------------------
    # NIFTY:   ±500 pts, rounded to 50
    # SENSEX:  ±1000 pts, rounded to 100
    nifty_strike_range_points: float = _env_float(
        "NIFTY_STRIKE_RANGE_POINTS", 500.0
    )
    sensex_strike_range_points: float = _env_float(
        "SENSEX_STRIKE_RANGE_POINTS", 1000.0
    )
    nifty_strike_rounding: float = _env_float(
        "NIFTY_STRIKE_ROUNDING", 50.0
    )
    sensex_strike_rounding: float = _env_float(
        "SENSEX_STRIKE_ROUNDING", 100.0
    )

    # Time (HH:MM IST) that separates "use first candle" from "use last
    # candle" when picking the reference close for the strike window.
    strike_range_mode_cutoff_time: str = _env_str(
        "STRIKE_RANGE_MODE_CUTOFF_TIME", "10:00"
    )

    # Static fallbacks — only used when the dynamic computation fails.
    nifty_strike_min: float = _env_float("NIFTY_STRIKE_MIN", 21000.0)
    nifty_strike_max: float = _env_float("NIFTY_STRIKE_MAX", 26000.0)
    sensex_strike_min: float = _env_float("SENSEX_STRIKE_MIN", 71000.0)
    sensex_strike_max: float = _env_float("SENSEX_STRIKE_MAX", 73000.0)

    # ---- EMA / crosses ------------------------------------------------
    ema_fast_period: int = int(os.getenv("EMA_FAST_PERIOD", "9"))
    ema_slow_period: int = int(os.getenv("EMA_SLOW_PERIOD", "21"))

    # ---- EMA validation (cross scoring) -------------------------------
    ema_enable_validation: bool = _env_bool("EMA_ENABLE_VALIDATION", True)
    ema_min_validation_score: int = _env_int("EMA_MIN_VALIDATION_SCORE", 3)

    ema_use_trend_filter: bool = _env_bool("EMA_USE_TREND_FILTER", True)
    ema_use_separation_filter: bool = _env_bool("EMA_USE_SEPARATION_FILTER", True)
    ema_use_slope_filter: bool = _env_bool("EMA_USE_SLOPE_FILTER", True)
    ema_use_volume_filter: bool = _env_bool("EMA_USE_VOLUME_FILTER", True)
    ema_use_body_filter: bool = _env_bool("EMA_USE_BODY_FILTER", True)
    ema_use_distance_filter: bool = _env_bool("EMA_USE_DISTANCE_FILTER", True)

    ema_trend_fast_period: int = _env_int("EMA_TREND_FAST_PERIOD", 50)
    ema_trend_slow_period: int = _env_int("EMA_TREND_SLOW_PERIOD", 100)
    ema_atr_period: int = _env_int("EMA_ATR_PERIOD", 14)
    ema_sep_atr_min: float = _env_float("EMA_SEP_ATR_MIN", 0.15)
    ema_slope_lookback: int = _env_int("EMA_SLOPE_LOOKBACK", 3)
    ema_volume_lookback: int = _env_int("EMA_VOLUME_LOOKBACK", 20)
    ema_volume_mult: float = _env_float("EMA_VOLUME_MULT", 1.0)
    ema_distance_atr_max: float = _env_float("EMA_DISTANCE_ATR_MAX", 1.5)

    # ---- MEGA backup --------------------------------------------------
    mega_enabled: bool = _env_bool("MEGA_ENABLED", True)
    mega_email: str = _env_str("MEGA_EMAIL", "")
    mega_password: str = _env_str("MEGA_PASSWORD", "")
    mega_backup_folder: str = _env_str(
        "MEGA_BACKUP_FOLDER", "novag7_candles_backup"
    )

    # ---- Telegram -----------------------------------------------------
    telegram_enabled: bool = _env_bool("TELEGRAM_ENABLED", False)
    telegram_bot_token: str = _env_str("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = _env_str("TELEGRAM_CHAT_ID", "")
    telegram_timeout_seconds: int = int(
        os.getenv("TELEGRAM_TIMEOUT_SECONDS", "10")
    )

    # ---- MongoDB (backup metadata/blob only) --------------------------
    mongo_enabled: bool = _env_bool("MONGO_ENABLED", False)
    mongo_url: str = _env_str("MONGO_URL", "")
    mongo_db_name: str = _env_str("MONGO_DB_NAME", "UPSTOX_APP_V2")
    mongo_metadata_collection: str = _env_str(
        "MONGO_METADATA_COLLECTION", "app_tokens"
    )
    mongo_metadata_doc_id: str = _env_str(
        "MONGO_METADATA_DOC_ID", "backup_candles"
    )
    mongo_blob_collection: str = _env_str(
        "MONGO_BLOB_COLLECTION", "backup_collection"
    )
    mongo_blob_doc_id: str = _env_str(
        "MONGO_BLOB_DOC_ID", "backup_historical_candles"
    )
    mongo_max_blob_mb: int = int(os.getenv("MONGO_MAX_BLOB_MB", "15"))
    mongo_timeout_ms: int = int(os.getenv("MONGO_TIMEOUT_MS", "10000"))

    # ---- Upstox SDK per-request timeouts ------------------------------
    upstox_request_timeout_seconds: float = _env_float(
        "UPSTOX_REQUEST_TIMEOUT_SECONDS", 60.0
    )
    upstox_connect_timeout_seconds: float = _env_float(
        "UPSTOX_CONNECT_TIMEOUT_SECONDS", 10.0
    )


settings = Settings()
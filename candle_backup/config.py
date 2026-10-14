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
    intraday_test_mode: bool = _env_bool("INTRADAY_TEST_MODE", False)

    # ---- Instrument archive (free, no auth) ---------------------------
    instruments_url: str = _env_str(
        "INSTRUMENTS_URL",
        "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz",
    )
    instruments_cache_dir: Path = BASE_DIR / os.getenv(
        "INSTRUMENTS_CACHE_DIR", "data/_cache"
    )

    # ---- Underlyings to back up (option chains) -----------------------
    nifty_underlying: str = _env_str("NIFTY_UNDERLYING", "NIFTY")
    sensex_underlying: str = _env_str("SENSEX_UNDERLYING", "SENSEX")

    nifty_strike_min: float = _env_float("NIFTY_STRIKE_MIN", 21000.0)
    nifty_strike_max: float = _env_float("NIFTY_STRIKE_MAX", 26000.0)
    sensex_strike_min: float = _env_float("SENSEX_STRIKE_MIN", 71000.0)
    sensex_strike_max: float = _env_float("SENSEX_STRIKE_MAX", 71000.0)

    # ---- EMA / crosses ------------------------------------------------
    ema_fast_period: int = int(os.getenv("EMA_FAST_PERIOD", "9"))
    ema_slow_period: int = int(os.getenv("EMA_SLOW_PERIOD", "21"))

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


settings = Settings()
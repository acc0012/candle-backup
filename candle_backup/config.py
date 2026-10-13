"""Configuration loaded from .env or the process environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Project root: candle-backup/
BASE_DIR = Path(__file__).resolve().parents[1]
ENV_FILE = BASE_DIR / ".env"

# Loads configuration from .env when it exists.
# Existing system environment variables are not overwritten.
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


@dataclass(frozen=True, slots=True)
class Settings:
    # Upstream subscriptions endpoint.
    subscriptions_api_url: str = os.getenv("SUBSCRIPTIONS_API_URL", "")
    request_timeout_seconds: float = float(
        os.getenv("REQUEST_TIMEOUT_SECONDS", "30")
    )

    # Logging.
    log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()
    log_file: Path = BASE_DIR / os.getenv("LOG_FILE", "logs/backup.log")

    # Candle cache.
    data_dir: Path = BASE_DIR / os.getenv("DATA_DIR", "data")
    history_lookback_days: int = int(os.getenv("HISTORY_LOOKBACK_DAYS", "10"))
    history_max_concurrency: int = int(
        os.getenv("HISTORY_MAX_CONCURRENCY", "8")
    )

    # Market window.
    market_timezone: str = os.getenv("MARKET_TIMEZONE", "Asia/Kolkata")
    market_open_time: str = os.getenv("MARKET_OPEN_TIME", "09:15")
    market_close_time: str = os.getenv("MARKET_CLOSE_TIME", "15:40")

    # MEGA backup.
    mega_enabled: bool = _env_bool("MEGA_ENABLED", True)
    mega_email: str = _env_str("MEGA_EMAIL", "")
    mega_password: str = _env_str("MEGA_PASSWORD", "")
    mega_backup_folder: str = _env_str(
        "MEGA_BACKUP_FOLDER", "novag7_candles_backup"
    )


settings = Settings()
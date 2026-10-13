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

    # ------------------------------------------------------------------
    # Historical completeness
    # ------------------------------------------------------------------
    # When true (default), the historical job never trusts the freshness
    # check alone. Every run re-fetches the full lookback window so the
    # backup uploaded to MEGA/Mongo always contains every market day,
    # with no internal gaps.
    #
    # When false, the job uses the incremental skip: if the local file
    # already contains data through the previous trading day, it is
    # skipped. That is faster but can miss days that were never fetched.
    #
    # Default: true. This is a backup job, not a latency-sensitive one —
    # the priority is completeness over speed.
    history_strict_completeness: bool = _env_bool(
        "HISTORY_STRICT_COMPLETENESS", True
    )

    # When strict completeness is on, each run re-fetches this many extra
    # days *before* the configured lookback window. Upstox occasionally
    # back-fills the most recent trading day after midnight, and the
    # overlap guard catches that. Set to 0 to disable overlap.
    history_verify_overlap_days: int = int(
        os.getenv("HISTORY_VERIFY_OVERLAP_DAYS", "1")
    )

    # Market window.
    market_timezone: str = os.getenv("MARKET_TIMEZONE", "Asia/Kolkata")
    market_open_time: str = os.getenv("MARKET_OPEN_TIME", "09:15")
    market_close_time: str = os.getenv("MARKET_CLOSE_TIME", "15:40")

    # Intraday test mode.
    intraday_test_mode: bool = _env_bool("INTRADAY_TEST_MODE", False)

    # MEGA backup.
    mega_enabled: bool = _env_bool("MEGA_ENABLED", True)
    mega_email: str = _env_str("MEGA_EMAIL", "")
    mega_password: str = _env_str("MEGA_PASSWORD", "")
    mega_backup_folder: str = _env_str(
        "MEGA_BACKUP_FOLDER", "novag7_candles_backup"
    )

    # Telegram notifications (optional, best-effort).
    telegram_enabled: bool = _env_bool("TELEGRAM_ENABLED", False)
    telegram_bot_token: str = _env_str("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = _env_str("TELEGRAM_CHAT_ID", "")
    telegram_timeout_seconds: int = int(
        os.getenv("TELEGRAM_TIMEOUT_SECONDS", "10")
    )

    # MongoDB (optional).
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
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

    # ------------------------------------------------------------------
    # MongoDB (optional)
    # ------------------------------------------------------------------
    # When enabled, the run metadata is upserted into a fixed document
    # inside MONGO_METADATA_COLLECTION, and — if the archive is small
    # enough — the archive bytes are upserted into a fixed document
    # inside MONGO_BLOB_COLLECTION. Both use fixed _id values so no new
    # documents are ever created across runs.
    mongo_enabled: bool = _env_bool("MONGO_ENABLED", False)

    # Full connection string, e.g.
    #   mongodb+srv://user:pass@cluster.example.net/?retryWrites=true
    mongo_url: str = _env_str("MONGO_URL", "")

    # Database and collection that hold the metadata document.
    mongo_db_name: str = _env_str("MONGO_DB_NAME", "UPSTOX_APP_V2")
    mongo_metadata_collection: str = _env_str(
        "MONGO_METADATA_COLLECTION", "app_tokens"
    )
    mongo_metadata_doc_id: str = _env_str(
        "MONGO_METADATA_DOC_ID", "backup_candles"
    )

    # Collection and doc that hold the archive bytes.
    mongo_blob_collection: str = _env_str(
        "MONGO_BLOB_COLLECTION", "backup_collection"
    )
    mongo_blob_doc_id: str = _env_str(
        "MONGO_BLOB_DOC_ID", "backup_historical_candles"
    )

    # Skip the blob save if the archive exceeds this size in MB.
    #
    # NOTE: MongoDB's hard BSON document limit is 16 MB. Setting this
    # above 16 is allowed but the write will be rejected by the server
    # for archives larger than ~15 MB. The error is caught and logged;
    # the run itself is not aborted.
    mongo_max_blob_mb: int = int(os.getenv("MONGO_MAX_BLOB_MB", "50"))

    # Server-selection / connect timeout for MongoClient, in ms.
    mongo_timeout_ms: int = int(os.getenv("MONGO_TIMEOUT_MS", "10000"))


settings = Settings()
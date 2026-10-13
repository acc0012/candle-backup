"""Persist backup metadata and (optionally) the archive to MongoDB.

Two fixed documents are upserted on every run — no new docs are ever
created:

* ``MONGO_METADATA_COLLECTION`` / ``MONGO_METADATA_DOC_ID``
    The full run metadata plus the MEGA public link of the archive.

* ``MONGO_BLOB_COLLECTION`` / ``MONGO_BLOB_DOC_ID``
    The archive bytes, stored as ``bson.Binary``. Only written when the
    archive is smaller than ``MONGO_MAX_BLOB_MB``.

Timestamps
----------
Every document carries two timestamp fields:

* ``updated_at``      — a human-readable India Standard Time string,
                        e.g. ``"2026-10-13 18:13:27 IST"``.
* ``updated_at_utc``  — the same instant as a timezone-aware UTC
                        ``datetime`` so it stays sortable / indexable
                        and can be range-queried.

Every function is best-effort: a MongoDB failure is logged and swallowed
so the backup itself never fails because of Mongo. All clients are
closed before returning.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from bson import Binary
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from candle_backup.config import settings
from candle_backup.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

# India Standard Time. Falls back to a fixed +05:30 offset if the
# tzdata package is missing on the host.
try:
    _IST = ZoneInfo("Asia/Kolkata")
except Exception:  # pragma: no cover — defensive
    from datetime import timedelta as _td
    _IST = timezone(_td(hours=5, minutes=30))


def _now_utc() -> datetime:
    """Timezone-aware UTC datetime for storage/indexing."""
    return datetime.now(timezone.utc)


def _now_ist_string() -> str:
    """Human-readable IST string, e.g. ``2026-10-13 18:13:27 IST``."""
    return datetime.now(_IST).strftime("%Y-%m-%d %H:%M:%S IST")


def _timestamp_fields() -> dict[str, Any]:
    """Return the timestamp pair every Mongo document receives."""
    return {
        "updated_at": _now_ist_string(),
        "updated_at_utc": _now_utc(),
    }


# ---------------------------------------------------------------------------
# Client helper
# ---------------------------------------------------------------------------

def _client() -> MongoClient | None:
    """Return a connected MongoClient or None when Mongo is disabled."""
    if not settings.mongo_enabled:
        return None

    if not settings.mongo_url:
        logger.warning(
            "MONGO_ENABLED=true but MONGO_URL is empty; skipping Mongo"
        )
        return None

    try:
        return MongoClient(
            settings.mongo_url,
            serverSelectionTimeoutMS=settings.mongo_timeout_ms,
            connectTimeoutMS=settings.mongo_timeout_ms,
        )
    except Exception:
        logger.exception("Unable to create MongoClient")
        return None


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def save_metadata(
    metadata: dict[str, Any],
    file_link: str | None,
) -> bool:
    """Upsert the run metadata into the fixed metadata document.

    Args:
        metadata:  any JSON-serialisable dict — typically the same one
                   that was written into the archive's
                   ``_metadata.json`` plus archive info from the
                   summary.
        file_link: public MEGA link to the uploaded archive, or None.

    Returns True on success, False when Mongo is disabled or the write
    failed.
    """
    client = _client()
    if client is None:
        return False

    doc = {
        "_id": settings.mongo_metadata_doc_id,
        **metadata,
        "file_link": file_link,
        **_timestamp_fields(),
    }

    try:
        collection = client[settings.mongo_db_name][
            settings.mongo_metadata_collection
        ]
        collection.update_one(
            {"_id": settings.mongo_metadata_doc_id},
            {"$set": doc},
            upsert=True,
        )
        logger.info(
            "Mongo metadata upserted; db=%s collection=%s doc=%s "
            "updated_at=%s",
            settings.mongo_db_name,
            settings.mongo_metadata_collection,
            settings.mongo_metadata_doc_id,
            doc["updated_at"],
        )
        return True
    except PyMongoError:
        logger.exception(
            "Mongo metadata upsert failed; db=%s collection=%s doc=%s",
            settings.mongo_db_name,
            settings.mongo_metadata_collection,
            settings.mongo_metadata_doc_id,
        )
        return False
    finally:
        try:
            client.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Blob
# ---------------------------------------------------------------------------

def save_blob_if_small(
    archive_path: Path,
    summary: dict[str, Any],
) -> dict[str, Any]:
    """Upsert the archive bytes into the fixed blob document.

    Returns a small status dict — always, never raises:

        {"saved": True,  "size_bytes": 12345}
        {"saved": False, "reason": "...", ...}
    """
    client = _client()
    if client is None:
        return {"saved": False, "reason": "mongo-disabled"}

    try:
        size = archive_path.stat().st_size
    except Exception:
        logger.exception("Unable to stat archive for Mongo blob save")
        return {"saved": False, "reason": "stat-failed"}

    limit_bytes = settings.mongo_max_blob_mb * 1024 * 1024
    if size > limit_bytes:
        logger.info(
            "Skipping Mongo blob save: archive %d bytes exceeds "
            "MONGO_MAX_BLOB_MB=%d (%d bytes)",
            size,
            settings.mongo_max_blob_mb,
            limit_bytes,
        )
        return {
            "saved": False,
            "reason": "too-large",
            "size_bytes": size,
            "limit_bytes": limit_bytes,
        }

    # Read the archive into memory. At ≤ 50 MB this is fine.
    try:
        data = archive_path.read_bytes()
    except Exception:
        logger.exception("Unable to read archive for Mongo blob save")
        return {"saved": False, "reason": "read-failed"}

    doc = {
        "_id": settings.mongo_blob_doc_id,
        "archive_name": summary.get("archive_name") or archive_path.name,
        "archive_size_bytes": size,
        "folder": summary.get("folder"),
        "file_link": summary.get("file_link"),
        "data": Binary(data),
        **_timestamp_fields(),
    }

    try:
        collection = client[settings.mongo_db_name][
            settings.mongo_blob_collection
        ]
        collection.update_one(
            {"_id": settings.mongo_blob_doc_id},
            {"$set": doc},
            upsert=True,
        )
        logger.info(
            "Mongo blob upserted; db=%s collection=%s doc=%s size=%d "
            "updated_at=%s",
            settings.mongo_db_name,
            settings.mongo_blob_collection,
            settings.mongo_blob_doc_id,
            size,
            doc["updated_at"],
        )
        return {"saved": True, "size_bytes": size}
    except PyMongoError as exc:
        message = str(exc)
        if "too large" in message.lower() or "10334" in message:
            logger.error(
                "Mongo rejected the archive as too large for a single "
                "document. MongoDB's BSON limit is 16 MB. Lower "
                "MONGO_MAX_BLOB_MB or use GridFS. Error: %s",
                message,
            )
        else:
            logger.exception(
                "Mongo blob upsert failed; db=%s collection=%s doc=%s",
                settings.mongo_db_name,
                settings.mongo_blob_collection,
                settings.mongo_blob_doc_id,
            )
        return {"saved": False, "reason": "mongo-error", "error": message}
    finally:
        try:
            client.close()
        except Exception:
            pass
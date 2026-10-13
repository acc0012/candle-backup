"""Persist backup metadata and (optionally) the archive to MongoDB.

Two fixed documents are upserted on every run — no new docs are ever
created:

* ``MONGO_METADATA_COLLECTION`` / ``MONGO_METADATA_DOC_ID``
    The full run metadata plus the MEGA public link of the archive.

* ``MONGO_BLOB_COLLECTION`` / ``MONGO_BLOB_DOC_ID``
    Either the archive bytes (stored as ``bson.Binary``) when the
    archive fits under ``MONGO_MAX_BLOB_MB``, or a metadata-only
    document that points the consumer at the MEGA share link when it
    does not.

The blob document is always present. Its shape depends on size:

    # Small enough — binary stored
    {
      _id: "backup_historical_candles",
      archive_name: "data.zip",
      archive_size_bytes: 2036147,
      data: BinData(0, "..."),
      file_link: "https://mega.co.nz/#!...",
      status: "saved",
      ...
    }

    # Too large — no binary, but metadata + link
    {
      _id: "backup_historical_candles",
      archive_name: "data.zip",
      archive_size_bytes: 62914560,
      data: null,
      file_link: "https://mega.co.nz/#!...",
      status: "skipped-size",
      note: "Backup archive (60.0 MB) exceeds the 15 MB Mongo blob limit. Download it through the shared link in 'file_link'.",
      ...
    }

Timestamps
----------
Every document carries two timestamp fields:

* ``updated_at``      — a human-readable India Standard Time string,
                        e.g. ``"2026-10-13 18:13:27 IST"``.
* ``updated_at_utc``  — the same instant as a timezone-aware UTC
                        ``datetime`` so it stays sortable / indexable.

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
# Size formatting
# ---------------------------------------------------------------------------

def _format_bytes(n: int | float | None) -> str:
    """Human-readable size, e.g. ``4.6 MB``."""
    if not isinstance(n, (int, float)) or n < 0:
        return "—"
    units = ["B", "KB", "MB", "GB"]
    size = float(n)
    i = 0
    while size >= 1024 and i < len(units) - 1:
        size /= 1024.0
        i += 1
    return f"{size:.1f} {units[i]}"


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
    """Upsert the run metadata into the fixed metadata document."""
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

def _base_blob_doc(
    archive_path: Path,
    summary: dict[str, Any],
) -> dict[str, Any]:
    """Return the metadata fields shared by every blob document variant."""
    size = summary.get("archive_size_bytes")
    if not isinstance(size, (int, float)):
        try:
            size = archive_path.stat().st_size
        except Exception:
            size = None

    return {
        "_id": settings.mongo_blob_doc_id,
        "archive_name": summary.get("archive_name") or archive_path.name,
        "archive_size_bytes": size,
        "archive_size_human": _format_bytes(size),
        "folder": summary.get("folder"),
        "file_link": summary.get("file_link"),
        "max_blob_mb": settings.mongo_max_blob_mb,
    }


def save_blob_if_small(
    archive_path: Path,
    summary: dict[str, Any],
) -> dict[str, Any]:
    """Upsert the blob document.

    The document is **always** written. When the archive fits under
    ``MONGO_MAX_BLOB_MB`` the binary is stored; when it does not, the
    document carries the same metadata plus a ``note`` explaining the
    skip and pointing at the MEGA share link.

    Returns a small status dict — always, never raises:

        {"saved": True,  "size_bytes": 12345, "status": "saved"}
        {"saved": False, "status": "skipped-size", "size_bytes": ...,
         "limit_bytes": ..., "note": "..."}
        {"saved": False, "status": "stat-failed" | "read-failed"
                                | "mongo-error" | "mongo-disabled"}
    """
    client = _client()
    if client is None:
        return {"saved": False, "status": "mongo-disabled"}

    # ---- Resolve the archive size -----------------------------------
    try:
        size = archive_path.stat().st_size
    except Exception:
        logger.exception("Unable to stat archive for Mongo blob save")
        return {"saved": False, "status": "stat-failed"}

    limit_bytes = settings.mongo_max_blob_mb * 1024 * 1024
    doc = _base_blob_doc(archive_path, summary)
    doc.update(_timestamp_fields())

    # ---- Branch on size ---------------------------------------------
    if size > limit_bytes:
        file_link = summary.get("file_link")

        note_lines = [
            f"Backup archive ({_format_bytes(size)}) exceeds the "
            f"{settings.mongo_max_blob_mb} MB Mongo blob limit.",
        ]
        if file_link:
            note_lines.append(
                "Download it through the shared link in 'file_link'."
            )
        else:
            note_lines.append(
                "No share link was captured for this run — check the "
                "MEGA backup folder directly."
            )

        doc.update(
            {
                "data": None,
                "status": "skipped-size",
                "reason": "size_limit_exceeded",
                "limit_bytes": limit_bytes,
                "limit_human": _format_bytes(limit_bytes),
                "note": " ".join(note_lines),
            }
        )

        try:
            collection = client[settings.mongo_db_name][
                settings.mongo_blob_collection
            ]
            collection.update_one(
                {"_id": settings.mongo_blob_doc_id},
                {"$set": doc},
                upsert=True,
            )
            logger.warning(
                "Mongo blob skipped (size %d bytes > %d bytes); "
                "metadata-only doc upserted with share link. "
                "db=%s collection=%s doc=%s",
                size,
                limit_bytes,
                settings.mongo_db_name,
                settings.mongo_blob_collection,
                settings.mongo_blob_doc_id,
            )
            return {
                "saved": False,
                "status": "skipped-size",
                "reason": "size_limit_exceeded",
                "size_bytes": size,
                "limit_bytes": limit_bytes,
                "file_link": file_link,
            }
        except PyMongoError:
            logger.exception(
                "Mongo metadata-only blob upsert failed; db=%s "
                "collection=%s doc=%s",
                settings.mongo_db_name,
                settings.mongo_blob_collection,
                settings.mongo_blob_doc_id,
            )
            return {
                "saved": False,
                "status": "mongo-error",
                "size_bytes": size,
                "limit_bytes": limit_bytes,
            }
        finally:
            try:
                client.close()
            except Exception:
                pass

    # ---- Archive fits — read it and store the binary ----------------
    try:
        data = archive_path.read_bytes()
    except Exception:
        logger.exception("Unable to read archive for Mongo blob save")
        return {"saved": False, "status": "read-failed"}

    doc.update(
        {
            "data": Binary(data),
            "status": "saved",
            "reason": None,
            "limit_bytes": limit_bytes,
            "limit_human": _format_bytes(limit_bytes),
            "note": (
                f"Backup archive stored inline ({_format_bytes(size)}). "
                "The same archive is also available via 'file_link'."
            ),
        }
    )

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
        return {"saved": True, "status": "saved", "size_bytes": size}
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
        return {
            "saved": False,
            "status": "mongo-error",
            "size_bytes": size,
            "error": message,
        }
    finally:
        try:
            client.close()
        except Exception:
            pass
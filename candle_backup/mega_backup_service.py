"""Upload the local candle cache to MEGA as a single zip archive.

Uses the published ``mega-sdk`` package (``pip install mega-sdk``) —
no vendored MEGA code lives in this repo.

Design
------
* The entire ``data/`` directory is compressed into a single ``data.zip``
  file and uploaded as one object.
* The zip contains the **contents** of ``data/`` directly at its root —
  ``index/``, ``nifty/``, ``sensex/``, etc. — not a wrapping ``data/``
  folder.
* Two provenance files are written into the archive before upload:
    - ``_manifest.txt``  — human-readable summary.
    - ``_metadata.json`` — structured run metadata.
* The remote folder is delete-then-recreate on every call so a stale
  archive never lingers next to the fresh one.
* The zip is created in a temporary directory that is removed as soon
  as the upload finishes. An optional ``post_upload`` callback is
  invoked while the archive still exists, so callers can persist the
  bytes (e.g. to MongoDB) before cleanup.
* Returns a summary dict so the caller can log or exit accordingly.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from mega_sdk import Mega
from candle_backup.config import settings
from candle_backup.logger import get_logger

logger = get_logger(__name__)

# A hook invoked after a successful upload but before the temp archive
# is deleted. Receives (archive_path, summary) — the path is valid for
# the duration of the call only.
PostUploadHook = Callable[[Path, dict[str, Any]], None]

# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _login() -> Mega:
    email = settings.mega_email
    password = settings.mega_password
    if not email or not password:
        raise RuntimeError("MEGA_EMAIL / MEGA_PASSWORD are not configured")

    logger.info("MEGA login as %s", email)
    mega = Mega()
    mega.login(email, password)
    mega.get_files()  # primes mega.root_id
    return mega

def _find_child_by_name(
    files: dict[str, Any], parent_id: str, name: str
) -> str | None:
    for handle, node in files.items():
        if node.get("p") != parent_id:
            continue
        attribs = node.get("a") or {}
        if attribs.get("n") == name:
            return handle
    return None

def _delete_remote_folder(mega: Mega, node_id: str) -> None:
    """Permanently remove a MEGA folder and its children."""
    logger.info("Deleting remote folder id=%s", node_id)
    try:
        mega.destroy(node_id)
    except Exception:
        logger.exception(
            "destroy failed for id=%s; falling back to trash + empty",
            node_id,
        )
        mega.delete(node_id)
        mega.empty_trash()

def _count_files(root: Path) -> int:
    return sum(1 for p in root.rglob("*") if p.is_file())

def _write_manifest(
    data_dir: Path,
    file_count: int,
    metadata: dict[str, Any],
) -> str:
    """Return the text content of ``_manifest.txt``."""
    return (
        "project=candle-backup\n"
        f"run_started_at={metadata.get('run_started_at', '')}\n"
        f"run_completed_at={metadata.get('run_completed_at', '')}\n"
        f"data_dir={data_dir.name}\n"
        f"file_count={file_count}\n"
    )

def _build_archive(
    data_dir: Path,
    temp_dir: Path,
    metadata: dict[str, Any],
) -> Path:
    """Zip the *contents* of ``data_dir`` into ``temp_dir/data.zip``.

    The archive is always named ``data.zip`` and contains the entries
    inside ``data_dir`` directly at its root::

        data.zip
        ├── index/
        ├── nifty/
        ├── sensex/
        ├── _manifest.txt
        └── _metadata.json
    """
    archive_base = temp_dir / "data"

    logger.info("Creating zip archive from %s", data_dir)

    archive_path = Path(
        shutil.make_archive(
            base_name=str(archive_base),
            format="zip",
            root_dir=str(data_dir),
        )
    )

    file_count = _count_files(data_dir)

    manifest_text = _write_manifest(data_dir, file_count, metadata)
    metadata_text = json.dumps(metadata, indent=2, ensure_ascii=False)

    with zipfile.ZipFile(archive_path, "a", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("_manifest.txt", manifest_text)
        zf.writestr("_metadata.json", metadata_text)

    return archive_path

def _get_uploaded_file_link(
    mega: Mega,
    folder_id: str,
    archive_name: str,
) -> str | None:
    """Return the public MEGA link of the just-uploaded archive.

    Best-effort: any failure is logged and None is returned.
    """
    try:
        files = mega.get_files()
    except Exception:
        logger.exception("Unable to refresh file list to obtain link")
        return None

    for handle, node in files.items():
        if node.get("p") != folder_id:
            continue
        if (node.get("a") or {}).get("n") != archive_name:
            continue
        try:
            return mega.get_link((handle, node))
        except Exception:
            logger.exception("mega.get_link failed for %s", archive_name)
            return None
    return None

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def backup_to_mega(
    folder: str | None = None,
    data_dir: Path | None = None,
    metadata: dict[str, Any] | None = None,
    post_upload: PostUploadHook | None = None,
) -> dict[str, Any]:
    """Zip ``data_dir`` into ``data.zip`` and upload it to MEGA.

    Args:
        folder:       remote folder name (defaults to settings).
        data_dir:     local source (defaults to ``settings.data_dir``).
        metadata:     optional run metadata. Merged with archive info
                      and written into the zip as ``_metadata.json``.
        post_upload:  optional callback invoked after a successful
                      upload but before the temp archive is deleted.
                      Receives ``(archive_path, summary)``. Any exception
                      it raises is logged and does not affect the
                      returned summary.

    The remote folder is deleted and recreated before the upload so a
    stale archive from a previous run never lingers alongside the new
    one. Returns a summary dict.
    """
    folder = folder or settings.mega_backup_folder
    data_dir = Path(data_dir or settings.data_dir)

    if not data_dir.exists():
        raise FileNotFoundError(f"Data dir does not exist: {data_dir}")

    file_count = _count_files(data_dir)
    if file_count == 0:
        raise RuntimeError(f"Data dir is empty: {data_dir}")

    # Seed metadata with fields only known here.
    archive_info: dict[str, Any] = {
        "project": "candle-backup",
        "archive_name": "data.zip",
        "archive_source_file_count": file_count,
    }
    metadata = {**(metadata or {}), **archive_info}

    summary: dict[str, Any] = {
        "folder": folder,
        "data_dir": str(data_dir),
        "archive_name": "data.zip",
        "archive_size_bytes": None,
        "source_file_count": file_count,
        "uploaded": False,
        "upload_error": None,
        "file_link": None,
        "completed_at": None,
    }

    with tempfile.TemporaryDirectory(prefix="candle_backup_") as tmp:
        archive_path = _build_archive(data_dir, Path(tmp), metadata)
        archive_name = archive_path.name
        archive_size = archive_path.stat().st_size
        summary["archive_size_bytes"] = archive_size

        logger.info(
            "Archive ready: %s (files=%d size=%d bytes)",
            archive_name,
            file_count,
            archive_size,
        )

        mega = _login()
        root_id = mega.root_id
        files = mega.get_files()

        existing = _find_child_by_name(files, root_id, folder)
        if existing:
            _delete_remote_folder(mega, existing)

        new_id = mega.create_folder_at(folder, root_id)
        logger.info("Created fresh remote folder %r id=%s", folder, new_id)

        try:
            mega.upload(
                str(archive_path),
                dest=new_id,
                dest_filename=archive_name,
            )
            summary["uploaded"] = True
        except Exception as exc:
            summary["upload_error"] = f"{type(exc).__name__}: {exc}"
            logger.exception("Upload failed: %s", archive_name)

        if summary["uploaded"]:
            summary["file_link"] = _get_uploaded_file_link(
                mega, new_id, archive_name
            )
            if summary["file_link"]:
                logger.info("MEGA public link: %s", summary["file_link"])
            else:
                logger.warning(
                    "Uploaded but could not obtain a public MEGA link"
                )

            if post_upload is not None:
                try:
                    post_upload(archive_path, summary)
                except Exception:
                    logger.exception("post_upload callback failed")

    summary["completed_at"] = _now_iso()
    logger.info("Backup summary: %s", summary)
    return summary
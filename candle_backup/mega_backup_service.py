"""Upload the local candle cache to MEGA as a single zip archive.

Uses the vendored ``mega_local`` package — no external MEGA SDK.

Design
------
* The entire ``data/`` directory is compressed into a single ``data.zip``
  file and uploaded as one object. This keeps the remote folder tidy and
  makes the backup trivially downloadable for consumers.
* The zip contains the **contents** of ``data/`` directly at its root —
  ``index/``, ``nifty/``, ``sensex/``, etc. — not a wrapping ``data/``
  folder. Downloading and unzipping gives the same layout as the local
  ``data/`` directory.
* The remote folder is delete-then-recreate on every call so a stale
  archive never lingers next to the fresh one.
* A small ``_manifest.txt`` is written **inside** the zip before upload
  so the archive is self-describing.
* The zip is created in a temporary directory that is removed as soon
  as the upload finishes, so nothing is left on disk.
* Returns a summary dict so the caller can log or exit accordingly.
"""

from __future__ import annotations

import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mega_local import Mega

from candle_backup.config import settings
from candle_backup.logger import get_logger

logger = get_logger(__name__)


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


def _build_archive(data_dir: Path, temp_dir: Path) -> Path:
    """Zip the *contents* of ``data_dir`` into ``temp_dir/data.zip``.

    The archive is always named ``data.zip`` and contains the entries
    inside ``data_dir`` directly at its root::

        data.zip
        ├── index/
        ├── nifty/
        ├── sensex/
        └── _manifest.txt

    A ``_manifest.txt`` entry is appended at the top level with the
    upload time, source directory name, and file count.
    """
    archive_base = temp_dir / "data"

    logger.info("Creating zip archive from %s", data_dir)

    # root_dir=data_dir, base_dir defaults to '.' which is interpreted
    # relative to root_dir — so the archive contains the *contents* of
    # data_dir, not a wrapping 'data' folder.
    archive_path = Path(
        shutil.make_archive(
            base_name=str(archive_base),
            format="zip",
            root_dir=str(data_dir),
        )
    )

    manifest = (
        "project=candle-backup\n"
        f"uploaded_at={datetime.now(timezone.utc).isoformat()}\n"
        f"data_dir={data_dir.name}\n"
        f"file_count={_count_files(data_dir)}\n"
    )
    with zipfile.ZipFile(archive_path, "a", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("_manifest.txt", manifest)

    return archive_path


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def backup_to_mega(
    folder: str | None = None,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """Zip ``data_dir`` into ``data.zip`` and upload it to MEGA.

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

    # Everything below happens inside a temporary directory that is
    # removed as soon as the ``with`` block exits — the .zip never
    # leaks into the repository.
    with tempfile.TemporaryDirectory(prefix="candle_backup_") as tmp:
        archive_path = _build_archive(data_dir, Path(tmp))
        archive_name = archive_path.name          # always "data.zip"
        archive_size = archive_path.stat().st_size

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

        uploaded = False
        upload_error: str | None = None
        try:
            mega.upload(
                str(archive_path),
                dest=new_id,
                dest_filename=archive_name,
            )
            uploaded = True
        except Exception as exc:
            upload_error = f"{type(exc).__name__}: {exc}"
            logger.exception("Upload failed: %s", archive_name)

    summary = {
        "folder": folder,
        "data_dir": str(data_dir),
        "archive_name": archive_name,
        "archive_size_bytes": archive_size,
        "source_file_count": file_count,
        "uploaded": uploaded,
        "upload_error": upload_error,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    logger.info("Backup summary: %s", summary)
    return summary
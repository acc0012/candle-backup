"""Upload the local candle cache to MEGA.

Uses the vendored ``mega_local`` package — no external MEGA SDK.

Design
------
* The remote folder is delete-then-recreate on every call so stale
  files never linger.
* Per-file failures are counted, not raised: one bad file does not
  abort the run.
* Returns a summary dict so the caller can log or exit accordingly.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mega_local import Mega

from candle_backup.config import settings
from candle_backup.logger import get_logger

logger = get_logger(__name__)


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


def _upload_tree(
    mega: Mega,
    local_dir: Path,
    remote_parent_id: str,
    summary: dict[str, int],
) -> None:
    for entry in sorted(local_dir.iterdir()):
        if entry.is_dir():
            try:
                child_id = mega.create_folder_at(entry.name, remote_parent_id)
            except Exception:
                logger.exception(
                    "Failed to create remote folder %r", entry.name
                )
                summary["folders_failed"] += 1
                continue
            _upload_tree(mega, entry, child_id, summary)
        elif entry.is_file():
            try:
                mega.upload(
                    str(entry),
                    dest=remote_parent_id,
                    dest_filename=entry.name,
                )
                summary["uploaded"] += 1
            except Exception:
                logger.exception("Upload failed: %s", entry)
                summary["upload_failed"] += 1


def backup_to_mega(
    folder: str | None = None,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """Upload ``data_dir`` to the MEGA account under ``folder``."""
    folder = folder or settings.mega_backup_folder
    data_dir = Path(data_dir or settings.data_dir)

    if not data_dir.exists():
        raise FileNotFoundError(f"Data dir does not exist: {data_dir}")

    mega = _login()
    root_id = mega.root_id
    files = mega.get_files()

    existing = _find_child_by_name(files, root_id, folder)
    if existing:
        _delete_remote_folder(mega, existing)

    new_id = mega.create_folder_at(folder, root_id)
    logger.info("Created fresh remote folder %r id=%s", folder, new_id)

    # Manifest alongside the tree.
    manifest_path = data_dir.parent / "_mega_backup_manifest.txt"
    manifest_path.write_text(
        "project=candle-backup\n"
        f"uploaded_at={datetime.now(timezone.utc).isoformat()}\n"
        f"data_dir={data_dir}\n",
        encoding="utf-8",
    )
    try:
        mega.upload(
            str(manifest_path),
            dest=new_id,
            dest_filename="_manifest.txt",
        )
    finally:
        manifest_path.unlink(missing_ok=True)

    summary = {
        "folder": folder,
        "data_dir": str(data_dir),
        "uploaded": 0,
        "upload_failed": 0,
        "folders_failed": 0,
    }
    _upload_tree(mega, data_dir, new_id, summary)
    summary["completed_at"] = datetime.now(timezone.utc).isoformat()
    logger.info("Backup summary: %s", summary)
    return summary
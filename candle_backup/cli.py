"""Programmatic entry point used by ``python -m candle_backup``."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from candle_backup.config import settings
from candle_backup.historical_candle_store import historical_candle_store
from candle_backup.logger import get_logger
from candle_backup.mega_backup_service import backup_to_mega
from candle_backup.subscription_store import subscription_store
from candle_backup.telegram_service import telegram_service

logger = get_logger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _do_fetch() -> tuple[int, dict]:
    """Run fetch jobs. Returns (exit_code, summary_dict)."""
    logger.info("Fetch: refreshing subscriptions")
    try:
        await subscription_store.refresh(reason="standalone-backup")
    except Exception as exc:
        logger.exception("Subscription refresh failed")
        return 2, {"stage": "subscriptions", "error": str(exc)}

    instruments = subscription_store.snapshot().get("active", []) or []
    logger.info("Fetch: %d active instruments", len(instruments))
    if not instruments:
        logger.error("No instruments returned; aborting")
        return 3, {"stage": "subscriptions", "error": "no instruments"}

    logger.info("Fetch: historical candles")
    hist = await historical_candle_store.ensure_recent_candles(
        instruments, reason="standalone-backup"
    )
    logger.info("Fetch: historical done %s", hist)

    logger.info("Fetch: intraday candles")
    intra = await historical_candle_store.ensure_intraday_candles(
        instruments, reason="standalone-backup"
    )
    logger.info("Fetch: intraday done %s", intra)

    return 0, {
        "instruments": len(instruments),
        "historical": hist,
        "intraday": intra,
    }


def _do_backup(metadata: dict) -> tuple[int, dict]:
    """Run the MEGA upload. Returns (exit_code, summary_dict)."""
    logger.info("Backup: zipping data/ and uploading to MEGA")
    try:
        summary = backup_to_mega(metadata=metadata)
    except Exception as exc:
        logger.exception("Backup failed")
        return 1, {"stage": "backup", "error": str(exc)}

    logger.info("Backup: done %s", summary)
    return (0 if summary.get("uploaded") else 1), summary


def _format_bytes(n: int | None) -> str:
    if not isinstance(n, int):
        return "—"
    units = ["B", "KB", "MB", "GB"]
    size = float(n)
    i = 0
    while size >= 1024 and i < len(units) - 1:
        size /= 1024.0
        i += 1
    return f"{size:.1f} {units[i]}"


def _build_metadata(
    run_started_at: str,
    fetch_summary: dict | None,
    backup_summary: dict | None,
    do_fetch: bool,
    do_backup: bool,
) -> dict:
    """Assemble the structured metadata written into data.zip.

    Keys are grouped by concern so consumers can navigate the JSON
    easily:

        run.*       — timestamps, duration, phases executed
        market.*    — timezone and window used by the intraday fetch
        fetch.*     — instruments count + per-job summaries
        backup.*    — remote folder and archive stats
    """
    run_completed_at = _now_iso()
    duration = None
    try:
        started = datetime.fromisoformat(run_started_at)
        completed = datetime.fromisoformat(run_completed_at)
        duration = round((completed - started).total_seconds(), 3)
    except Exception:
        duration = None

    metadata: dict = {
        "project": "candle-backup",
        "run": {
            "started_at": run_started_at,
            "completed_at": run_completed_at,
            "duration_seconds": duration,
            "fetch_phase": "enabled" if do_fetch else "disabled",
            "backup_phase": "enabled" if do_backup else "disabled",
        },
        "market": {
            "timezone": settings.market_timezone,
            "open_time": settings.market_open_time,
            "close_time": settings.market_close_time,
            "intraday_test_mode": settings.intraday_test_mode,
        },
    }

    if fetch_summary:
        metadata["fetch"] = {
            "instruments_total": fetch_summary.get("instruments"),
            "historical": fetch_summary.get("historical"),
            "intraday": fetch_summary.get("intraday"),
        }
    else:
        metadata["fetch"] = None

    if backup_summary:
        metadata["backup"] = {
            "folder": backup_summary.get("folder"),
            "archive_name": backup_summary.get("archive_name"),
            "archive_size_bytes": backup_summary.get("archive_size_bytes"),
            "source_file_count": backup_summary.get("source_file_count"),
            "uploaded": backup_summary.get("uploaded"),
        }
    else:
        metadata["backup"] = None

    return metadata


async def run(do_fetch: bool, do_backup: bool) -> int:
    run_started_at = _now_iso()

    # ---- Telegram: start ------------------------------------------------
    try:
        telegram_service.notify_start(
            {
                "project": "candle-backup",
                "fetch": "enabled" if do_fetch else "disabled",
                "backup": "enabled" if do_backup else "disabled",
                "started_at": run_started_at,
            }
        )
    except Exception:
        logger.exception("Telegram start notification failed")

    exit_code = 0
    failure_error: str | None = None
    fetch_summary: dict | None = None
    backup_summary: dict | None = None

    # ---- Run ------------------------------------------------------------
    try:
        if do_fetch:
            rc, fetch_summary = await _do_fetch()
            if rc != 0:
                exit_code = rc
                failure_error = (
                    f"fetch stage failed: "
                    f"{fetch_summary.get('error', 'unknown')}"
                )
                raise RuntimeError(failure_error)

        if do_backup:
            # Build the metadata *before* the upload so the zip carries
            # the same information the summary reports.
            metadata = _build_metadata(
                run_started_at=run_started_at,
                fetch_summary=fetch_summary,
                backup_summary=None,
                do_fetch=do_fetch,
                do_backup=do_backup,
            )
            rc, backup_summary = await asyncio.to_thread(_do_backup, metadata)
            if rc != 0:
                exit_code = rc
                failure_error = (
                    backup_summary.get("upload_error")
                    or f"backup stage failed (rc={rc})"
                )
                raise RuntimeError(failure_error)

    except Exception as exc:
        # Already logged above by the inner step; this catches anything
        # that escaped and sends the Telegram failure notification.
        if failure_error is None:
            failure_error = f"{type(exc).__name__}: {exc}"
            logger.exception("Unhandled exception during run")

        elapsed = (datetime.now(timezone.utc) -
                   datetime.fromisoformat(run_started_at)).total_seconds()
        try:
            telegram_service.notify_failure(
                failure_error,
                {
                    "project": "candle-backup",
                    "elapsed_seconds": f"{elapsed:.1f}",
                    "failed_at": _now_iso(),
                },
            )
        except Exception:
            logger.exception("Telegram failure notification failed")

        return exit_code or 1

    # ---- Telegram: success ----------------------------------------------
    elapsed = (datetime.now(timezone.utc) -
               datetime.fromisoformat(run_started_at)).total_seconds()

    details: dict[str, object] = {
        "project": "candle-backup",
        "elapsed_seconds": f"{elapsed:.1f}",
        "completed_at": _now_iso(),
    }

    if backup_summary:
        details["archive"] = backup_summary.get("archive_name")
        details["archive_size"] = _format_bytes(
            backup_summary.get("archive_size_bytes")
        )
        details["source_files"] = backup_summary.get("source_file_count")
        details["folder"] = backup_summary.get("folder")

    try:
        telegram_service.notify_success(details)
    except Exception:
        logger.exception("Telegram success notification failed")

    return 0
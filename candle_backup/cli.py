"""Programmatic entry point used by ``python -m candle_backup``."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from candle_backup.historical_candle_store import historical_candle_store
from candle_backup.logger import get_logger
from candle_backup.mega_backup_service import backup_to_mega
from candle_backup.subscription_store import subscription_store
from candle_backup.telegram_service import telegram_service

logger = get_logger(__name__)


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


def _do_backup() -> tuple[int, dict]:
    """Run the MEGA upload. Returns (exit_code, summary_dict)."""
    logger.info("Backup: zipping data/ and uploading to MEGA")
    try:
        summary = backup_to_mega()
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


async def run(do_fetch: bool, do_backup: bool) -> int:
    started_at = datetime.now(timezone.utc)

    # ---- Telegram: start ------------------------------------------------
    try:
        telegram_service.notify_start(
            {
                "project": "candle-backup",
                "fetch": "enabled" if do_fetch else "disabled",
                "backup": "enabled" if do_backup else "disabled",
                "started_at": started_at.isoformat(),
            }
        )
    except Exception:
        logger.exception("Telegram start notification failed")

    exit_code = 0
    failure_error: str | None = None

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
            rc, backup_summary = await asyncio.to_thread(_do_backup)
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

        elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
        try:
            telegram_service.notify_failure(
                failure_error,
                {
                    "project": "candle-backup",
                    "elapsed_seconds": f"{elapsed:.1f}",
                    "failed_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception:
            logger.exception("Telegram failure notification failed")

        return exit_code or 1

    # ---- Telegram: success ----------------------------------------------
    elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()

    details: dict[str, object] = {
        "project": "candle-backup",
        "elapsed_seconds": f"{elapsed:.1f}",
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }

    if do_backup:
        # Attach the archive info produced by the backup step.
        try:
            details["archive"] = backup_summary.get("archive_name")  # type: ignore[name-defined]
            details["archive_size"] = _format_bytes(
                backup_summary.get("archive_size_bytes")  # type: ignore[name-defined]
            )
            details["source_files"] = backup_summary.get(  # type: ignore[name-defined]
                "source_file_count"
            )
            details["folder"] = backup_summary.get("folder")  # type: ignore[name-defined]
        except NameError:
            pass

    try:
        telegram_service.notify_success(details)
    except Exception:
        logger.exception("Telegram success notification failed")

    return 0
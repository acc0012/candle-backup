"""Programmatic entry point used by ``python -m candle_backup``."""

from __future__ import annotations

import asyncio

from candle_backup.historical_candle_store import historical_candle_store
from candle_backup.logger import get_logger
from candle_backup.mega_backup_service import backup_to_mega
from candle_backup.subscription_store import subscription_store

logger = get_logger(__name__)


async def _do_fetch() -> int:
    logger.info("Fetch: refreshing subscriptions")
    try:
        await subscription_store.refresh(reason="standalone-backup")
    except Exception:
        logger.exception("Subscription refresh failed")
        return 2

    instruments = subscription_store.snapshot().get("active", []) or []
    logger.info("Fetch: %d active instruments", len(instruments))
    if not instruments:
        logger.error("No instruments returned; aborting")
        return 3

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

    return 0


def _do_backup() -> int:
    logger.info("Backup: uploading to MEGA")
    summary = backup_to_mega()
    logger.info("Backup: done %s", summary)
    return 0 if summary.get("upload_failed", 0) == 0 else 1


async def run(do_fetch: bool, do_backup: bool) -> int:
    if do_fetch:
        rc = await _do_fetch()
        if rc != 0:
            return rc

    if do_backup:
        rc = await asyncio.to_thread(_do_backup)
        if rc != 0:
            return rc

    return 0
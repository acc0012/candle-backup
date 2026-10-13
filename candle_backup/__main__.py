"""Entry point: ``python -m candle_backup``.

Flags:
    --fetch-only   Skip the MEGA upload.
    --backup-only  Skip the fetch (upload whatever is in data/ already).
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from candle_backup.cli import run


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="candle_backup",
        description=(
            "Fetch 1-minute candles for every subscribed instrument and "
            "upload the local data/ directory to MEGA."
        ),
    )
    parser.add_argument(
        "--fetch-only",
        action="store_true",
        help="Only fetch candles; skip the MEGA upload.",
    )
    parser.add_argument(
        "--backup-only",
        action="store_true",
        help="Only upload the existing data/ directory; skip the fetch.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()

    if args.fetch_only and args.backup_only:
        print(
            "error: --fetch-only and --backup-only are mutually exclusive",
            file=sys.stderr,
        )
        return 2

    return asyncio.run(
        run(
            do_fetch=not args.backup_only,
            do_backup=not args.fetch_only,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
# Candle Backup

Standalone tool that fetches 1-minute candles for every subscribed
instrument and uploads the local `data/` directory to MEGA.

Runs on a schedule from GitHub Actions (05:00 IST, Monday–Saturday) and
can also be triggered manually.

## What it does

1. Fetch the instrument list from the upstream subscriptions endpoint.
2. For each instrument, ensure the last `HISTORY_LOOKBACK_DAYS` of
   1-minute candles are cached locally under `data/`.
3. Fetch today's intraday candles (skipped automatically when the
   current time is outside the configured market window).
4. Log in to MEGA, delete the remote folder if it exists, recreate it,
   and upload the whole `data/` tree.

No EMA, no WebSockets, no FastAPI. Just candles and upload.

## Layout on disk

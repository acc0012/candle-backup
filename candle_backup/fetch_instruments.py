import os
import gzip
import json
import requests
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any, List, Dict

# Upstox complete instruments archive
UPSTOX_URL = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
MARKET_TZ = ZoneInfo("Asia/Kolkata")

def parse_expiry_to_dt(expiry_val: Any) -> datetime | None:
    """
    Parses expiry epoch millisecond (or second) integer into an IST datetime.
    """
    if expiry_val is None:
        return None
    try:
        ts = float(expiry_val)
        # Check if timestamp is in milliseconds (> 10^11) or seconds
        if ts > 1e11:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=MARKET_TZ)
    except (ValueError, TypeError, OSError):
        return None

def get_market_cutoff_time(now_dt: datetime) -> datetime:
    """
    Returns the market close cutoff datetime (15:30:00 IST) for today.
    If the current time is past 15:30 IST on the expiry day,
    that day's contracts are considered expired and skipped.
    """
    return now_dt.replace(hour=15, minute=30, second=0, microsecond=0)

def fetch_upstox_instruments(url: str = UPSTOX_URL, temp_gz: str = "temp_complete.json.gz") -> List[Dict[str, Any]]:
    """
    Downloads the gzipped instrument list, parses it into memory,
    and removes the temporary compressed file.
    """
    print(f"Downloading instrument archive from: {url}")
    response = requests.get(url, stream=True, timeout=60)
    response.raise_for_status()

    with open(temp_gz, "wb") as f:
        for chunk in response.iter_content(chunk_size=65536):
            f.write(chunk)

    print("Extracting and parsing JSON data...")
    with gzip.open(temp_gz, "rt", encoding="utf-8") as f:
        data = json.load(f)

    if os.path.exists(temp_gz):
        os.remove(temp_gz)

    print(f"Loaded {len(data)} total instruments.")
    return data

def filter_nearest_expiry_options(
    instruments: List[Dict[str, Any]],
    underlyings: List[str],
    output_dir: str = "data",
    market_close_hour: int = 15,
    market_close_minute: int = 30
) -> Dict[str, Dict[str, Any]]:
    """
    Filters the nearest expiry options (CE/PE) for the specified underlyings.
    Saves results to: <output_dir>/<underlying_lower>/options.json
    Returns a dictionary mapping underlying -> filtered summary and contracts.
    """
    now = datetime.now(MARKET_TZ)
    cutoff_today = now.replace(
        hour=market_close_hour,
        minute=market_close_minute,
        second=0,
        microsecond=0
    )

    # Normalize targets, e.g. ["nifty", "sensex"]
    target_underlyings = {u.strip().upper(): u.strip().lower() for u in underlyings}
    results = {}

    for target_upper, target_folder in target_underlyings.items():
        # Step 1: Collect all option contracts for this underlying
        option_candidates = []
        for item in instruments:
            if not isinstance(item, dict):
                continue

            # Match only option contracts
            inst_type = str(item.get("instrument_type") or "").upper()
            if inst_type not in ("CE", "PE"):
                continue

            # Match underlying name or symbol
            item_underlying = str(
                item.get("underlying_symbol")
                or item.get("name")
                or item.get("asset_symbol")
                or ""
            ).strip().upper()

            if item_underlying != target_upper:
                continue

            expiry_dt = parse_expiry_to_dt(item.get("expiry"))
            if expiry_dt is None:
                continue

            # Step 2: Skip expired contracts
            # If expiry date is strictly in the past, skip
            if expiry_dt.date() < now.date():
                continue

            # If expiry is today, but time is past 15:30 IST, roll over to next expiry
            if expiry_dt.date() == now.date() and now >= cutoff_today:
                continue

            option_candidates.append((expiry_dt, item))

        if not option_candidates:
            print(f"[{target_upper}] No active upcoming options found.")
            continue

        # Step 3: Identify the nearest expiry date
        all_expiries = sorted(list({exp_dt.date() for exp_dt, _ in option_candidates}))
        nearest_expiry_date = all_expiries[0]

        # Step 4: Filter all contracts that expire on this nearest date
        nearest_contracts = [
            item for exp_dt, item in option_candidates
            if exp_dt.date() == nearest_expiry_date
        ]

        # Sort by strike_price and then instrument_type
        nearest_contracts.sort(
            key=lambda x: (
                float(x.get("strike_price") or 0),
                str(x.get("instrument_type") or "")
            )
        )

        folder_path = Path(output_dir) / target_folder
        folder_path.mkdir(parents=True, exist_ok=True)
        file_path = folder_path / "options.json"

        output_payload = {
            "underlying": target_upper,
            "nearest_expiry_date": nearest_expiry_date.strftime("%Y-%m-%d"),
            "evaluated_at_ist": now.strftime("%Y-%m-%d %H:%M:%S IST"),
            "total_options": len(nearest_contracts),
            "options": nearest_contracts
        }

        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(output_payload, f, indent=2)

        print(
            f"[{target_upper}] Nearest Expiry: {nearest_expiry_date} "
            f"| Saved {len(nearest_contracts)} contracts -> {file_path}"
        )

        results[target_upper] = output_payload

    return results

def run_pipeline(underlyings: List[str] = ["NIFTY", "SENSEX", "BANKNIFTY"]):
    """
    Full workflow: downloads archive, applies expiry & underlying filters,
    and saves files into individual folders.
    """
    instruments = fetch_upstox_instruments()
    return filter_nearest_expiry_options(instruments, underlyings=underlyings)

# if __name__ == "__main__":
#     # Example: specify which underlyings you want to extract
#     target_list = ["NIFTY", "SENSEX"]
#     run_pipeline(target_list)
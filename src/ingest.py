"""
ingest.py
=========
Download raw German power data from the Energy-Charts API (Fraunhofer ISE).

No API key is required. We pull three things:

  1. Day-ahead price        -> /price          (EUR/MWh)
  2. Generation + load      -> /public_power    (wind, solar, load)
  3. Cross-border flows     -> /cbpf            (net imports/exports)

Each series is saved as a Parquet file in data/raw/ so later steps (and re-runs)
do not have to hit the API again.

API docs: https://api.energy-charts.info  (interactive Swagger page)

Run from the project root with:   python -m src.ingest
"""

import time
from datetime import date

import pandas as pd
import requests

from config import (API_BASE, END_DATE, MARKET_BZN, MARKET_COUNTRY,
                    POWER_SERIES, RAW_DIR, START_DATE)

# Be polite to the free public API: small pause between calls, and retry if we
# get a 429 ("Too Many Requests") instead of crashing.
REQUEST_DELAY_SEC = 2.0
MAX_RETRIES = 5


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _get_json(endpoint, params):
    """Call one Energy-Charts endpoint and return the parsed JSON response.

    Handles rate limiting (HTTP 429) with an exponential back-off retry.
    """
    url = f"{API_BASE}/{endpoint}"
    for attempt in range(MAX_RETRIES):
        resp = requests.get(url, params=params, timeout=60)
        if resp.status_code == 429:
            # Honour the server's Retry-After header if present, else back off.
            wait = int(resp.headers.get("Retry-After", 5 * (attempt + 1)))
            print(f"    rate limited (429), waiting {wait}s and retrying...")
            time.sleep(wait)
            continue
        resp.raise_for_status()      # raise on any other error
        time.sleep(REQUEST_DELAY_SEC)  # gentle pause before the next call
        return resp.json()
    # If we exhausted the retries, raise the last error loudly.
    resp.raise_for_status()


def _year_chunks(start, end):
    """Yield (start, end) ISO date strings one calendar year at a time.

    Long date ranges can be slow, so we download year by year and stitch
    the pieces together afterwards.
    """
    chunk_start = start
    while chunk_start < end:
        year_end = date(chunk_start.year, 12, 31)
        chunk_end = min(year_end, end)
        yield chunk_start.isoformat(), chunk_end.isoformat()
        chunk_start = date(chunk_start.year + 1, 1, 1)


def _unix_to_utc(unix_seconds):
    """Convert a list of Unix timestamps (always UTC) to UTC datetimes."""
    return pd.to_datetime(unix_seconds, unit="s", utc=True)


def _tidy(frame_list):
    """Combine yearly chunks: stack, drop duplicate timestamps, sort by time."""
    out = pd.concat(frame_list, ignore_index=True)
    out = out.drop_duplicates("timestamp_utc").sort_values("timestamp_utc")
    return out.reset_index(drop=True)


# ---------------------------------------------------------------------------
# One function per data source
# ---------------------------------------------------------------------------
def fetch_price():
    """Day-ahead electricity price for the DE-LU bidding zone (EUR/MWh)."""
    frames = []
    for start, end in _year_chunks(START_DATE, END_DATE):
        print(f"  price        {start} -> {end}")
        data = _get_json("price", {"bzn": MARKET_BZN, "start": start, "end": end})
        frames.append(pd.DataFrame({
            "timestamp_utc": _unix_to_utc(data["unix_seconds"]),
            "price_eur_mwh": data["price"],
        }))
    return _tidy(frames)


def fetch_public_power():
    """Generation by source and load; keep only the series we care about."""
    frames = []
    for start, end in _year_chunks(START_DATE, END_DATE):
        print(f"  public_power {start} -> {end}")
        data = _get_json("public_power",
                         {"country": MARKET_COUNTRY, "start": start, "end": end})
        df = pd.DataFrame({"timestamp_utc": _unix_to_utc(data["unix_seconds"])})
        # production_types is a list of {"name": ..., "data": [...]}.
        by_name = {p["name"]: p["data"] for p in data["production_types"]}
        for name in POWER_SERIES:
            df[name] = by_name.get(name)   # missing series -> column of NaN
        frames.append(df)
    return _tidy(frames)


def fetch_cbpf():
    """Cross-border physical flows; keep the net total ('sum')."""
    frames = []
    for start, end in _year_chunks(START_DATE, END_DATE):
        print(f"  cbpf         {start} -> {end}")
        data = _get_json("cbpf",
                         {"country": MARKET_COUNTRY, "start": start, "end": end})
        by_name = {c["name"]: c["data"] for c in data["countries"]}
        frames.append(pd.DataFrame({
            "timestamp_utc": _unix_to_utc(data["unix_seconds"]),
            # 'sum' = net physical flow across all borders (sign verified in QA)
            "net_flow_mw": by_name["sum"],
        }))
    return _tidy(frames)


# Day-ahead-style forecasts used as LEAKAGE-FREE model features (Task 2).
# We use forecast_type="current" = the archived pre-delivery forecast for each
# timestamp (a forecast, NOT the actuals). NOTE: Energy-Charts does not provide a
# keyless LOAD forecast, so we only fetch wind + solar here; load's predictable
# shape is captured later via calendar features instead.
FORECAST_SERIES = {
    "wind_onshore_fcst_mw": "wind_onshore",
    "wind_offshore_fcst_mw": "wind_offshore",
    "solar_fcst_mw": "solar",
}


def fetch_forecasts():
    """Pre-delivery forecasts for wind + solar (features for the price model).

    These are forecasts known *before* delivery, so using them to predict the
    day-ahead price does not leak future information.
    """
    frames = []
    for start, end in _year_chunks(START_DATE, END_DATE):
        # Pull each series for this year, then merge them on the timestamp.
        chunk = None
        for col, prod_type in FORECAST_SERIES.items():
            print(f"  forecast {prod_type:14s} {start} -> {end}")
            data = _get_json("public_power_forecast",
                             {"country": MARKET_COUNTRY, "production_type": prod_type,
                              "forecast_type": "current", "start": start, "end": end})
            times = data["unix_seconds"]
            values = data["forecast_values"]
            # Be defensive: if the API returns no/short values, pad with NaN so
            # the columns always line up with the timestamps.
            if len(values) != len(times):
                values = (values + [None] * len(times))[:len(times)]
            part = pd.DataFrame({
                "timestamp_utc": _unix_to_utc(times),
                col: values,
            })
            chunk = part if chunk is None else chunk.merge(part, on="timestamp_utc", how="outer")
        frames.append(chunk)
    return _tidy(frames)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def _download(name, fetch_func, filename):
    """Download one series unless its Parquet file already exists (caching)."""
    path = RAW_DIR / filename
    if path.exists():
        print(f"{name}: already cached at {path}, skipping.")
        return
    print(f"{name}:")
    fetch_func().to_parquet(path, index=False)


def main():
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {START_DATE} -> {END_DATE} for {MARKET_BZN}\n")

    _download("Day-ahead price", fetch_price, "price.parquet")
    _download("Public power (generation + load)", fetch_public_power, "public_power.parquet")
    _download("Cross-border flows", fetch_cbpf, "cbpf.parquet")
    _download("Day-ahead forecasts (load, wind, solar)", fetch_forecasts, "forecast.parquet")

    print(f"\nDone. Raw Parquet files written to {RAW_DIR}")


if __name__ == "__main__":
    main()

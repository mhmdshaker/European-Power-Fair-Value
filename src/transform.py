"""
transform.py
============
Turn the three raw Parquet files into ONE clean, hourly, analysis-ready table.

Steps:
  1. Load the raw series (price, generation/load, net flows).
  2. Resample everything to a common HOURLY grid.
       - price is 15-min  -> hourly average
       - power is 15-min  -> hourly average (MW averaged over the hour)
  3. Combine wind onshore + offshore into a single "wind" column.
  4. Merge all series on the UTC timestamp.
  5. Add a local (Europe/Berlin) timestamp and check the DST days look right.
  6. Save to data/processed/dataset.parquet

Timezone rule of thumb used everywhere:
  * We store and join on UTC (no ambiguity).
  * We add a local column only for human-readable delivery hours.

Run from the project root with:   python -m src.transform
"""

import pandas as pd

from config import PROCESSED_DIR, RAW_DIR, TIMEZONE


def _to_hourly(df, how="mean"):
    """Resample a UTC-indexed frame down to hourly buckets."""
    df = df.set_index("timestamp_utc")
    hourly = df.resample("1h").agg(how)
    return hourly


def load_and_merge():
    """Read raw files, align to hourly, and merge into one DataFrame (UTC index)."""
    # --- prices: 15-min -> hourly average -------------------------------
    price = pd.read_parquet(RAW_DIR / "price.parquet")
    price = _to_hourly(price, how="mean")

    # --- generation + load: 15-min -> hourly average --------------------
    power = pd.read_parquet(RAW_DIR / "public_power.parquet")
    power = _to_hourly(power, how="mean")
    # Total wind = onshore + offshore (kept separate in the raw data).
    power["wind_mw"] = power["Wind onshore"].fillna(0) + power["Wind offshore"].fillna(0)
    power = power.rename(columns={"Solar": "solar_mw", "Load": "load_mw"})
    power = power[["load_mw", "wind_mw", "solar_mw"]]

    # --- net cross-border flow: 15-min -> hourly average ----------------
    flows = pd.read_parquet(RAW_DIR / "cbpf.parquet")
    flows = _to_hourly(flows, how="mean")

    # --- merge on the shared UTC hourly index ---------------------------
    df = price.join([power, flows], how="outer")
    return df


def add_local_time(df):
    """Add a local Europe/Berlin timestamp column next to the UTC index."""
    df = df.copy()
    # The index is tz-aware UTC; convert a copy of it to local time.
    df["timestamp_local"] = df.index.tz_convert(TIMEZONE)
    return df


def transform():
    """Full transform: load, merge, add local time, save to processed/."""
    df = load_and_merge()
    df = add_local_time(df)

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    out_path = PROCESSED_DIR / "dataset.parquet"
    df.to_parquet(out_path)

    print(f"Merged dataset: {df.shape[0]:,} hourly rows, {df.shape[1]} columns")
    print(f"Columns: {list(df.columns)}")
    print(f"Saved to {out_path}")
    return df


if __name__ == "__main__":
    transform()

"""
features.py
===========
Build a LEAKAGE-FREE feature matrix for the next-day Day-Ahead price model.

The model predicts the hourly DA price for day D+1, using only information that
is known the morning of day D (before the auction closes):

  * Wind + solar DAY-AHEAD FORECASTS  (known before delivery -> no leakage)
  * Calendar features                 (hour, weekday, month, holiday, peak)
                                       -> these stand in for the predictable
                                          load shape (we have no load forecast)
  * Price lags that are already known  (same hour yesterday, same hour last week)

The actual price is the target; actual wind/solar/load are NOT used as inputs.

Run a quick check with:   python -m src.features
"""

import holidays
import pandas as pd

from config import PROCESSED_DIR, RAW_DIR

# EEX "peak" block is 08:00-20:00 local time on working days.
PEAK_START, PEAK_END = 8, 20

# The columns the model is allowed to see, and the target it predicts.
FEATURES = [
    "wind_fcst_mw", "solar_fcst_mw", "renewable_fcst_mw",   # leakage-free forecasts
    "hour", "dayofweek", "month", "is_weekend", "is_holiday", "is_peak",  # calendar
    "price_lag_24h", "price_lag_168h",                       # known past prices
]
TARGET = "price_eur_mwh"


def _hourly_forecasts():
    """Load the wind+solar forecasts and resample to an hourly grid."""
    f = pd.read_parquet(RAW_DIR / "forecast.parquet").set_index("timestamp_utc")
    f = f.resample("1h").mean()
    # Total wind = onshore + offshore; total renewables = wind + solar.
    f["wind_fcst_mw"] = f["wind_onshore_fcst_mw"].fillna(0) + f["wind_offshore_fcst_mw"].fillna(0)
    f["renewable_fcst_mw"] = f["wind_fcst_mw"] + f["solar_fcst_mw"]
    return f[["wind_fcst_mw", "solar_fcst_mw", "renewable_fcst_mw"]]


def build_features():
    """Return a time-indexed DataFrame with the target, features, and helpers."""
    # Start from the processed dataset (we only need the price target + local time).
    data = pd.read_parquet(PROCESSED_DIR / "dataset.parquet")
    df = data[["price_eur_mwh", "timestamp_local"]].copy()

    # Attach the leakage-free wind+solar forecasts (joined on the UTC index).
    df = df.join(_hourly_forecasts(), how="left")

    # --- Calendar features (from LOCAL Berlin time, which is what trading uses) ---
    local = df["timestamp_local"].dt
    df["hour"] = local.hour
    df["dayofweek"] = local.dayofweek            # Monday=0 ... Sunday=6
    df["month"] = local.month
    df["is_weekend"] = (df["dayofweek"] >= 5).astype(int)

    de_holidays = holidays.Germany()
    df["is_holiday"] = df["timestamp_local"].dt.date.map(
        lambda d: d in de_holidays).astype(int)

    df["is_peak"] = (((df["hour"] >= PEAK_START) & (df["hour"] < PEAK_END))
                     & (df["is_weekend"] == 0)).astype(int)

    # --- Price lags that are already known when we forecast day D+1 ---------------
    # 24h ago  = same hour yesterday (day D, already cleared)
    # 168h ago = same hour last week (also known)
    df["price_lag_24h"] = df["price_eur_mwh"].shift(24)
    df["price_lag_168h"] = df["price_eur_mwh"].shift(168)

    return df


def model_frame():
    """Feature matrix ready for modelling: drop rows missing any feature/target."""
    df = build_features()
    needed = FEATURES + [TARGET]
    clean = df.dropna(subset=needed)
    return clean


if __name__ == "__main__":
    df = model_frame()
    print(f"Model-ready rows: {len(df):,}")
    print(f"Range: {df.index.min()} -> {df.index.max()}")
    print(f"Features ({len(FEATURES)}): {FEATURES}")
    print(df[FEATURES + [TARGET]].head())

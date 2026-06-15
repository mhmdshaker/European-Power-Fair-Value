"""
Central configuration for the European Power Fair Value project.

Everything you might want to tweak (market, dates, folders) lives here so the
rest of the code can simply `from config import ...`.
"""

from datetime import date
from pathlib import Path

# --- Market -----------------------------------------------------------------
MARKET_BZN = "DE-LU"          # bidding zone used for day-ahead PRICES
MARKET_COUNTRY = "de"         # country code used for GENERATION / LOAD / FLOWS
TIMEZONE = "Europe/Berlin"    # local delivery-time zone for Germany (CET/CEST)

# --- Date range (~3 years of history) ---------------------------------------
END_DATE = date.today()
START_DATE = date(END_DATE.year - 3, END_DATE.month, END_DATE.day)

# --- Which series to keep from the /public_power endpoint --------------------
# Germany shut down its last nuclear plants in April 2023, so we rely on
# renewables + load (plus net flows from a separate endpoint).
POWER_SERIES = ["Wind onshore", "Wind offshore", "Solar", "Load"]

# --- Energy-Charts API (no key required) ------------------------------------
API_BASE = "https://api.energy-charts.info"

# --- Folder layout ----------------------------------------------------------
ROOT = Path(__file__).resolve().parent
RAW_DIR = ROOT / "data" / "raw"             # one Parquet per raw series
PROCESSED_DIR = ROOT / "data" / "processed" # final merged hourly dataset
OUTPUT_DIR = ROOT / "outputs"               # QA report + figures
FIGURE_DIR = OUTPUT_DIR / "figures"

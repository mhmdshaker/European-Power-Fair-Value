# European Power Fair Value — Task 1: Data Ingestion + Data Quality

A reproducible pipeline that builds a clean, hourly dataset for the **German
(DE-LU) power market** from **public, key-free** data, and runs data-quality
checks on it.

This is the data foundation for a larger project: forecasting Day-Ahead prices
from fundamentals and translating that view into prompt-curve positioning.

---

## What it builds

A single hourly table for Germany covering ~3 years, with:

| Column | Meaning | Unit |
|---|---|---|
| `price_eur_mwh` | Day-Ahead price (DE-LU bidding zone) | EUR/MWh |
| `load_mw` | Electricity demand (load) | MW |
| `wind_mw` | Wind generation (onshore + offshore) | MW |
| `solar_mw` | Solar generation | MW |
| `net_flow_mw` | Net cross-border physical flow | MW |
| `timestamp_local` | Local delivery time (Europe/Berlin) | — |

(The table is indexed by UTC timestamp.)

---

## Data source

All data comes from the **Energy-Charts API** by **Fraunhofer ISE** — public,
free, and **no API key required**. Interactive docs: <https://api.energy-charts.info>

| Series | Endpoint | Notes |
|---|---|---|
| Day-Ahead price | `GET /price?bzn=DE-LU` | 15-min, resampled to hourly |
| Load, wind, solar | `GET /public_power?country=de` | 15-min, resampled to hourly |
| Net cross-border flow | `GET /cbpf?country=de` | uses the `"sum"` series |

Upstream sources are ENTSO-E / EEX / Bundesnetzagentur (SMARD), licensed CC BY 4.0.

---

## Setup

```bash
# 1. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt
```

> Note: if `python` is aliased to your system Python, call the venv directly
> with `.venv/bin/python` (used in the commands below).

---

## Run the pipeline

```bash
# 1. Download raw data from Energy-Charts  -> data/raw/*.parquet
.venv/bin/python -m src.ingest

# 2. Clean, align to an hourly grid, merge  -> data/processed/dataset.parquet
.venv/bin/python -m src.transform

# 3. Run data-quality checks + figures      -> outputs/qa_report.md, outputs/figures/
.venv/bin/python -m src.qa
```

Or explore everything interactively in `notebooks/01_data_qa.ipynb`.

---

## Project layout

```
.
├── config.py             # market, dates, folders (edit here)
├── src/
│   ├── ingest.py         # download raw series from Energy-Charts
│   ├── transform.py      # timezone/DST handling, hourly alignment, merge
│   └── qa.py             # data-quality checks + figures
├── notebooks/
│   └── 01_data_qa.ipynb  # end-to-end walkthrough
├── data/
│   ├── raw/              # cached API pulls (Parquet)
│   └── processed/        # final merged dataset (Parquet)
├── outputs/
│   ├── qa_report.md      # data-quality summary
│   └── figures/          # QA figures (PNG)
└── reports/              # the written submission document
```

---

## How timezones & DST are handled

- The API returns timestamps as **Unix seconds (UTC)** — unambiguous.
- We **store and join everything on UTC**, then add a local `Europe/Berlin`
  column only for human-readable delivery hours.
- The QA step **verifies** the daylight-saving days: the spring-forward date
  should contain **23 hours** and the autumn fall-back date **25 hours**.

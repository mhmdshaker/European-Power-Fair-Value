# European Power Fair Value (German DA Power)

A reproducible pipeline for the **German (DE-LU) power market** using **public,
key-free** data. It:

1. **Task 1** — builds a clean hourly dataset + data-quality checks.
2. **Task 2** — forecasts next-day hourly Day-Ahead prices (leakage-free) and
   aggregates to next-week / next-month block fair values for the prompt curve.
3. **Task 3** — translates the forecast into a tradable prompt-curve view
   (fair value + bands → edge vs anchor → signal + invalidation rules).

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
#    (prices, load/wind/solar actuals, flows, AND wind/solar day-ahead forecasts)
.venv/bin/python -m src.ingest

# 2. Clean, align to an hourly grid, merge  -> data/processed/dataset.parquet
.venv/bin/python -m src.transform

# 3. Run data-quality checks + figures      -> outputs/qa_report.md, outputs/figures/
.venv/bin/python -m src.qa

# 4. Forecast + validate + curve view       -> outputs/forecast_metrics.md, submission.csv
.venv/bin/python -m src.forecast

# 5. Prompt-curve translation + signals      -> outputs/curve_view.md, figures/curve_view.png
.venv/bin/python -m src.curve
```

Or explore interactively: `notebooks/01_data_qa.ipynb` (Task 1),
`notebooks/02_forecasting.ipynb` (Task 2), `notebooks/03_curve.ipynb` (Task 3).

---

## Project layout

```
.
├── config.py             # market, dates, folders (edit here)
├── src/
│   ├── ingest.py         # download raw series + forecasts from Energy-Charts
│   ├── transform.py      # timezone/DST handling, hourly alignment, merge
│   ├── qa.py             # data-quality checks + figures
│   ├── features.py       # leakage-free feature matrix for the price model
│   ├── forecast.py       # baselines + HGB, walk-forward CV, curve view, submission
│   └── curve.py          # prompt-curve translation: fair value -> signal + invalidation
├── notebooks/
│   ├── 01_data_qa.ipynb      # Task 1 walkthrough
│   ├── 02_forecasting.ipynb  # Task 2 walkthrough
│   └── 03_curve.ipynb        # Task 3 walkthrough
├── data/
│   ├── raw/              # cached API pulls (Parquet)
│   └── processed/        # final merged dataset (Parquet)
├── outputs/
│   ├── qa_report.md          # data-quality summary
│   ├── forecast_metrics.md   # CV + test metrics + curve view
│   ├── curve_view.md         # tradable signal + invalidation rules
│   └── figures/              # QA + forecast + curve figures (PNG)
├── submission.csv        # out-of-sample predictions (id, y_pred)
└── reports/              # the written submission document
```

---

## How timezones & DST are handled

- The API returns timestamps as **Unix seconds (UTC)** — unambiguous.
- We **store and join everything on UTC**, then add a local `Europe/Berlin`
  column only for human-readable delivery hours.
- The QA step **verifies** the daylight-saving days: the spring-forward date
  should contain **23 hours** and the autumn fall-back date **25 hours**.

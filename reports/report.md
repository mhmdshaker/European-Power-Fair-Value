# European Power Fair Value — Task 1: Data Ingestion & Data Quality

**Author:** Mohamad Shaker  <!-- TODO: confirm full name -->
**Email:** shaker.mohamad01@gmail.com
**Market:** Germany (DE-LU)
**Date:** 2026-06-16

---

## 1. Objective

Build a reproducible, fundamentals-grounded dataset for one European power market
using only public, key-free sources, with correct timezone/DST handling and an
auditable data-quality (QA) layer. This is the foundation for later Day-Ahead
forecasting and prompt-curve work.

## 2. Market and drivers

I chose **Germany (DE-LU)** — the most liquid European market, with the largest
renewable share, which makes its day-ahead price strongly driven by fundamentals
(and frequently negative). I ingest the **day-ahead price** plus four fundamental
drivers:

| Field | Driver role |
|---|---|
| `load_mw` | Demand — baseline price level |
| `wind_mw` (onshore + offshore) | Largest source of price variance / negative prices |
| `solar_mw` | Daily price-shape driver |
| `net_flow_mw` | Net cross-border physical flow — marginal supply/demand |

Note: Germany shut down its last nuclear plants in **April 2023**, so nuclear is
not a usable driver for this period — confirming the renewables + flows choice.

## 3. Data source (public, no API key)

All data is from the **Energy-Charts API by Fraunhofer ISE**
(<https://api.energy-charts.info>, interactive Swagger docs). It is free and
requires no key; upstream data is ENTSO-E / EEX / Bundesnetzagentur (SMARD),
licensed CC BY 4.0.

| Series | Endpoint | Native granularity |
|---|---|---|
| Day-Ahead price | `GET /price?bzn=DE-LU` | 15-min |
| Load, wind, solar | `GET /public_power?country=de` | 15-min |
| Net cross-border flow | `GET /cbpf?country=de` (`"sum"` series) | 15-min |

Engineering notes: requests are pulled **year-by-year and cached** to Parquet, with
**retry/back-off on HTTP 429** rate limits to be polite to the free API.

## 4. Timezone & DST handling

- The API returns timestamps as **Unix seconds (UTC)** — unambiguous.
- The pipeline **stores and joins everything on UTC**, then adds a local
  `Europe/Berlin` column only for human-readable delivery hours.
- All 15-min series are resampled to a common **hourly** grid (mean), which is the
  resolution of the modelling target.

**DST validation (key correctness check):** a correct UTC->local conversion must
yield a 23-hour day each spring (clocks forward) and a 25-hour day each autumn
(clocks back). The QA step confirms exactly this for all 3 years:

| Date | Hours in day | Transition |
|---|---|---|
| 2023-10-29 | 25 | autumn fall-back |
| 2024-03-31 | 23 | spring forward |
| 2024-10-27 | 25 | autumn fall-back |
| 2025-03-30 | 23 | spring forward |
| 2025-10-26 | 25 | autumn fall-back |
| 2026-03-29 | 23 | spring forward |

## 5. Data-quality results

Final dataset: **26,328 hourly rows**, 2023-06-15 → 2026-06-16.

- **Coverage:** 100.0% of expected hours present (26,328 / 26,328).
- **Duplicates:** 0 duplicated timestamps.
- **Missingness:** price 0%; load/wind/solar/flows each ~0.1% (≈25–27 hours,
  the most recent/not-yet-published periods).
- **Outliers (flagged, not dropped):**
  - 1,537 negative-price hours — these are **real** in Germany (oversupply), so
    they are flagged and kept, not removed. Price floor hit at **-500 EUR/MWh**
    (the EPEX limit), max 936 EUR/MWh.
  - Wind/solar generation: no negative values.
  - Load range 30.9–78.2 GW — within sensible German bounds.

### Figures
- `outputs/figures/missingness.png` — % missing per field.
- `outputs/figures/price_vs_renewables.png` — daily price vs. wind+solar, showing
  the merit-order effect (price falls as renewables rise).

## 6. Reproducibility

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m src.ingest        # download -> data/raw/
python -m src.transform     # clean + align -> data/processed/dataset.parquet
python -m src.qa            # checks + figures -> outputs/
```

Or run `notebooks/01_data_qa.ipynb` end to end.

---

# Task 2: Forecasting & Model Validation

## 1. Target & justification (Option A)

Forecast **next-day hourly Day-Ahead prices**, then aggregate to **base/peak
blocks** and **next-week / next-month averages**. The hourly price is the atomic
unit: one model serves both DA trading and the curve view, because blocks and
period averages are just aggregations of it. Modelling blocks directly (Option B)
would discard the intra-day shape that drives peak/base spreads.

## 2. Leakage control (the key design point)

At prediction time (morning of day D, before the auction for D+1), we do **not**
know tomorrow's actual wind/solar/load. So features use only information known
then:

- **Wind + solar day-ahead forecasts** (leakage-free; from Energy-Charts
  `/public_power_forecast`, `forecast_type=current` = the archived pre-delivery
  forecast, *not* actuals).
- **Calendar** (hour, weekday, month, weekend, German holidays, peak flag) — this
  captures load's predictable shape. *Note:* Energy-Charts exposes no keyless
  **load** forecast, so rather than leak actual load, we let calendar features
  stand in for it.
- **Price lags already known**: same hour yesterday (`lag 24h`) and same hour last
  week (`lag 168h`).

Actual generation/load are never used as model inputs. Transforms are fit inside
each CV fold; the test window is never seen during selection.

## 3. Models

| Model | Role |
|---|---|
| `seasonal_naive` (price = same hour last week) | Baseline (strong for power) |
| `linear` (ordinary least squares) | Baseline |
| `hgb` (HistGradientBoostingRegressor) | Improved model |

## 4. Validation & results

**Expanding-window walk-forward CV** — 6 folds × 30 days; each fold trains only on
data *before* its validation block. The model is selected on CV (HGB), **not** on
the test set.

CV (mean over folds):

| model | MAE | RMSE | tail_MAE |
|---|---|---|---|
| seasonal_naive | 35.36 | 52.80 | 74.04 |
| linear | 19.44 | 28.74 | 39.09 |
| **hgb** | **16.27** | **24.66** | **35.25** |

Held-out test (last 30 days, out-of-sample):

| model | MAE | RMSE | tail_MAE |
|---|---|---|---|
| seasonal_naive | 31.50 | 46.19 | 54.92 |
| linear | 18.72 | 27.71 | 34.20 |
| hgb | 19.60 | 27.82 | 46.06 |

**Tail metric** = MAE on the most extreme 5% of hours (spikes + deep-negative).
HGB cuts CV MAE ~54% vs the seasonal-naive baseline. On the final test month the
linear model edged HGB — but model choice is made on CV (more robust over 6
windows), which is the methodologically correct rule.

## 5. DA → curve view

The HGB hourly forecast is aggregated to base/peak block averages; an uncertainty
band comes from a **block bootstrap of out-of-sample daily forecast errors**
(sampling whole days respects error correlation and avoids a CLT variance
collapse). For the test window:

| Horizon | Baseload fcst | Baseload P10/P50/P90 | Baseload actual |
|---|---|---|---|
| Next week (7d) | 95.70 | 97.82 / 104.41 / 110.96 | 107.75 |
| Next month (30d) | 77.65 | 83.38 / 86.47 / 89.69 | 94.47 |

This is the bridge to prompt-curve positioning (Task 3): an expected block fair
value plus a distribution to size risk against the traded curve.

## 6. Honest limitations

- **Level bias:** HGB tracks the daily shape well but **under-forecasts** in this
  high-price test month (see `outputs/figures/forecast_vs_actual.png`); the
  bootstrap P50 bias-corrects partially. Calibration/quantile modelling is a
  clear next step.
- **No load forecast:** calendar proxies load; a keyless SMARD load forecast would
  restore an explicit residual-load feature.
- **Fuel prices** (gas, carbon) are not yet features — they set the marginal cost
  and would likely fix much of the level bias.

## 7. Reproducibility (Task 2)

```bash
python -m src.ingest        # now also pulls wind/solar forecasts
python -m src.transform
python -m src.forecast      # CV + test + figures + submission.csv
```
Or run `notebooks/02_forecasting.ipynb`. Out-of-sample predictions are in
`submission.csv` (id = UTC timestamp, y_pred = forecast EUR/MWh).

## 8. Next steps

- **Task 3 — DA → curve translation:** map the fair-value view onto prompt-curve
  positioning with explicit invalidation logic.

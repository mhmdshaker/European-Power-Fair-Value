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

## 7. Next steps

- **Task 2 — Forecasting:** model Day-Ahead price from these fundamentals with
  proper time-series validation, baselines, and leakage avoidance.
- **Task 3 — DA → curve translation:** map the fair-value view onto prompt-curve
  positioning with explicit invalidation logic.

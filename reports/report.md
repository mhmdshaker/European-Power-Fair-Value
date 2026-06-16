# European Power Fair Value — German Day-Ahead → Prompt Curve

**Author:** Mohamad Shaker  **Email:** shaker.mohamad01@gmail.com
**Market:** Germany (DE-LU)  **Date:** 2026-06-16

A reproducible prototype that builds a fundamentals-grounded daily fair-value view
for German power and translates it into prompt-curve positioning. All data is
**public and key-free**. Detailed numbers live in `outputs/*.md`; each task has a
runnable notebook (`notebooks/0X_*.ipynb`).

---

## 1. Data ingestion & data quality

**Source:** Energy-Charts API (Fraunhofer ISE) — public, no key
(<https://api.energy-charts.info>; upstream ENTSO-E/EEX/SMARD, CC BY 4.0). Endpoints:
`/price` (day-ahead price), `/public_power` (load, wind, solar actuals), `/cbpf`
(net cross-border flow), `/public_power_forecast` (wind/solar day-ahead forecasts).

**Timezone/DST:** all timestamps stored/joined in **UTC**, with a local
`Europe/Berlin` column for delivery hours. The 15-min series are resampled to a
clean hourly grid.

**QA results** (~3 years, `outputs/qa_report.md` + 2 figures):

| Check | Result |
|---|---|
| Coverage | 100.0% (26,328 / 26,328 hours), **0 duplicates** |
| Missingness | price 0%, drivers ~0.1% |
| DST verification | 23-hour spring days & 25-hour autumn days confirmed for all 3 years |
| Outliers | 1,537 negative-price hours **flagged, not dropped** (real in DE; floor −500); load within 30.9–78.2 GW |

---

## 2. Forecasting & model validation (Option A)

Forecast **next-day hourly Day-Ahead prices**, then aggregate to base/peak blocks
and week/month averages — one model serves both DA trading and the curve view.

**Leakage control (key design point):** features use only what is known the
morning of day D — **wind+solar day-ahead forecasts**, **calendar** (hour, weekday,
month, holiday, peak; this stands in for load, since no keyless load forecast
exists), and **already-known price lags** (24h, 168h). Actual generation/load are
never inputs. CV transforms are fit inside folds; the test set is untouched during
selection.

**Validation:** expanding-window walk-forward (6 folds × 30 days) + a held-out
last-30-day out-of-sample test. Metrics: MAE, RMSE, and a **tail metric** (MAE on
the most extreme 5% of hours).

CV (mean over folds), EUR/MWh:

| model | MAE | RMSE | tail_MAE |
|---|---|---|---|
| seasonal_naive (baseline) | 35.36 | 52.80 | 74.04 |
| linear (baseline) | 19.44 | 28.74 | 39.09 |
| **HGB (improved)** | **16.27** | **24.66** | **35.25** |

HGB cuts MAE **~54%** vs the (deliberately strong) seasonal-naive baseline. Model
choice is made on CV, not the test set. Out-of-sample predictions are in
`submission.csv` (`id` = UTC timestamp, `y_pred`). Full detail:
`outputs/forecast_metrics.md`.

---

## 3. Prompt-curve translation

**Method:** hourly forecast → prompt-month **fair value** (base + peak) with
**P10/P50/P90 bands** (block bootstrap of out-of-sample daily errors) → **edge** vs
a market anchor (trailing-30-day realised average — a transparent proxy; a live
forward quote drops in unchanged) → **confidence-weighted signal**
(`z = edge / band`, sized to a desk clip; edges inside the band are not traded).

**Worked example** (`outputs/curve_view.md`): anchor 84.44, fair value P50 86.47,
edge +2.03 vs band half-width 3.16 → **level FLAT** (edge inside noise, stand
aside); **shape SHORT peak/base spread** (summer solar belly).

**Desk use:** level → buy/sell prompt-month baseload; shape → peak/off-peak spread;
aggregate up for prompt-quarter and calendar spreads.

**Invalidation (computed):** (1) realised average leaves P10–P90 → recalibrate;
(2) renewables drift >15% from forecast → fair value stale; (3) edge < band → no
trade; (4) gas/carbon shock (manual gate). In the example, band-breach **FAILED**
(actual 94.47 > P90 89.69) while renewable-drift **PASSED** (+0.7%) — together
diagnosing the miss as a fuel/carbon effect, not renewables.

---

## 4. AI-accelerated workflow (programmatic)

`src/ai_commentary.py` generates the **daily desk "drivers" note** with an LLM,
from our exact computed metrics — automating a manual writing task.

- **Called from code** (Anthropic SDK); key via `ANTHROPIC_API_KEY` **env var only**
  (`.env` git-ignored, `.env.example` provided; no secret committed or logged).
- **Grounded:** the model is fed only our metrics JSON and told to invent no numbers.
- **Hallucination guard:** `verify_numbers()` flags any output number not grounded
  in the metrics → rejects it for a safe fallback. (Verified: invented `42.0/150.0`
  are caught.)
- **Logged:** prompt, output, verification, and failure mode →
  `outputs/ai_logs/commentary_log.jsonl`. Failure modes handled: `no_api_key`,
  `api_error`, `hallucination_flagged`, `ok`. Output: `outputs/daily_commentary.md`.

---

## Reproducibility

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m src.ingest      # download (keyless)        -> data/raw/
python -m src.transform   # clean + align hourly      -> data/processed/
python -m src.qa          # data-quality + figures    -> outputs/
python -m src.forecast    # CV + test + submission.csv
python -m src.curve       # tradable signal + invalidation
python -m src.ai_commentary  # AI note (optional key)
```

## Honest limitations & next steps

- **Level bias:** HGB tracks daily shape well but under-forecasts in the high-price
  test month; add **gas/carbon** features and quantile calibration.
- **No keyless load forecast:** calendar proxies load; a SMARD load forecast would
  restore an explicit residual-load feature.
- The market anchor is a proxy; plug in live forward quotes for production signals.

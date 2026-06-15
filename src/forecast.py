"""
forecast.py
===========
Next-day hourly Day-Ahead price model, with honest time-series validation.

Models (Task 2 asks for >=1 baseline and >=1 improved model):
  * seasonal_naive  - baseline: price = same hour last week  (strong on power)
  * linear          - baseline: ordinary linear regression on the features
  * hgb             - improved: gradient boosting (HistGradientBoostingRegressor)

Validation:
  * Expanding-window WALK-FORWARD CV (each validation block is predicted using
    only data from before it -> no future leakage).
  * The most recent ~30 days are held out entirely as an out-of-sample TEST set,
    used only for final metrics and submission.csv.

Metrics: MAE, RMSE, and a tail metric (MAE on the most extreme 5% of hours).

DA -> curve: the hourly forecasts are aggregated to base/peak block averages, and
a residual bootstrap gives an expected-average distribution (P10/P50/P90) for the
period — the bridge to prompt-curve positioning (Task 3).

Run with:   python -m src.forecast
"""

import matplotlib
matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_absolute_error, mean_squared_error

from config import FIGURE_DIR, OUTPUT_DIR, ROOT
from src.features import FEATURES, TARGET, model_frame

# --- Validation setup -------------------------------------------------------
TEST_DAYS = 30      # held-out out-of-sample window (for submission.csv)
CV_FOLDS = 6        # number of walk-forward folds
FOLD_DAYS = 30      # length of each validation block
MODELS = ["seasonal_naive", "linear", "hgb"]


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
def compute_metrics(y_true, y_pred):
    """MAE, RMSE, and tail MAE (on the most extreme 5% of actual prices)."""
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    mae = mean_absolute_error(y_true, y_pred)
    rmse = mean_squared_error(y_true, y_pred) ** 0.5
    lo, hi = np.quantile(y_true, [0.05, 0.95])
    tail = (y_true <= lo) | (y_true >= hi)   # spikes and deep-negative hours
    tail_mae = mean_absolute_error(y_true[tail], y_pred[tail])
    return {"MAE": round(mae, 2), "RMSE": round(rmse, 2), "tail_MAE": round(tail_mae, 2)}


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
def fit_predict(name, train, test):
    """Train model `name` on `train` and return its predictions for `test`."""
    if name == "seasonal_naive":
        # No fitting: predict the price from the same hour one week earlier.
        return test["price_lag_168h"].to_numpy()
    if name == "linear":
        model = LinearRegression()
    elif name == "hgb":
        model = HistGradientBoostingRegressor(
            max_iter=400, learning_rate=0.05, random_state=0)
    else:
        raise ValueError(f"unknown model: {name}")
    model.fit(train[FEATURES], train[TARGET])
    return model.predict(test[FEATURES])


# ---------------------------------------------------------------------------
# Splits: walk-forward folds + final held-out test
# ---------------------------------------------------------------------------
def make_splits(df):
    """Return (trainval, test, folds) where folds are expanding-window CV blocks."""
    df = df.sort_index()
    test_start = df.index.max() - pd.Timedelta(days=TEST_DAYS)
    trainval = df[df.index <= test_start]
    test = df[df.index > test_start]

    folds = []
    for i in range(CV_FOLDS):
        val_end = test_start - pd.Timedelta(days=FOLD_DAYS * i)
        val_start = val_end - pd.Timedelta(days=FOLD_DAYS)
        val = trainval[(trainval.index > val_start) & (trainval.index <= val_end)]
        train = trainval[trainval.index <= val_start]   # only the past
        folds.append((train, val))
    return trainval, test, folds


def cross_validate(df):
    """Run every model across all walk-forward folds.

    Returns the tidy metrics table, plus the HGB out-of-sample predictions
    (used later to estimate honest forecast uncertainty for the curve view).
    """
    _, _, folds = make_splits(df)
    rows = []
    oos_parts = []   # HGB validation predictions (genuinely out-of-sample)
    for name in MODELS:
        for k, (train, val) in enumerate(folds, start=1):
            preds = fit_predict(name, train, val)
            m = compute_metrics(val[TARGET], preds)
            m.update({"model": name, "fold": k, "val_start": val.index.min().date()})
            rows.append(m)
            if name == "hgb":
                oos_parts.append(pd.DataFrame(
                    {"actual": val[TARGET].to_numpy(), "pred": preds}, index=val.index))
    oos = pd.concat(oos_parts).sort_index()
    return pd.DataFrame(rows), oos


# ---------------------------------------------------------------------------
# Final test + submission
# ---------------------------------------------------------------------------
def final_test(df):
    """Train on everything up to the test window, evaluate all models on it."""
    trainval, test, _ = make_splits(df)
    results, preds_by_model = [], {}
    for name in MODELS:
        preds = fit_predict(name, trainval, test)
        preds_by_model[name] = preds
        m = compute_metrics(test[TARGET], preds)
        m["model"] = name
        results.append(m)
    return test, preds_by_model, pd.DataFrame(results)


def write_submission(test, preds):
    """Write submission.csv with id (UTC timestamp) and y_pred for the test window."""
    sub = pd.DataFrame({
        "id": test.index.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "y_pred": np.round(preds, 2),
    })
    path = ROOT / "submission.csv"
    sub.to_csv(path, index=False)
    return path


# ---------------------------------------------------------------------------
# DA -> curve: block averages + bootstrap distribution
# ---------------------------------------------------------------------------
def daily_residuals(oos):
    """Daily-average forecast errors (actual - pred) from out-of-sample CV."""
    err = (oos["actual"] - oos["pred"]).to_frame("err")
    return err["err"].resample("1D").mean().dropna().to_numpy()


def curve_view(test, preds, daily_resid, horizon_days, n_boot=5000):
    """Forecast the average price of a block (base + peak) over the next
    `horizon_days`, with an uncertainty band.

    * Expected average = mean of the hourly forecasts over the horizon.
    * Uncertainty      = BLOCK bootstrap of the out-of-sample daily errors
                         (sampling whole days respects error correlation, and
                         avoids the CLT-collapse of independent hourly draws).
    """
    df = pd.DataFrame({"actual": test[TARGET].to_numpy(), "pred": np.asarray(preds),
                       "is_peak": test["is_peak"].to_numpy()}, index=test.index)
    df = df[df.index < df.index.min() + pd.Timedelta(days=horizon_days)]

    base_fc, base_act = df["pred"].mean(), df["actual"].mean()
    peak = df[df["is_peak"] == 1]
    peak_fc, peak_act = peak["pred"].mean(), peak["actual"].mean()

    # Distribution of the horizon-average price: forecast + resampled daily errors.
    n_days = max(1, round(len(df) / 24))
    rng = np.random.default_rng(0)
    sims = [base_fc + rng.choice(daily_resid, size=n_days, replace=True).mean()
            for _ in range(n_boot)]
    p10, p50, p90 = np.quantile(sims, [0.10, 0.50, 0.90])
    r = lambda x: round(float(x), 2)   # plain Python float for clean output
    return {
        "baseload_forecast": r(base_fc), "baseload_actual": r(base_act),
        "peak_forecast": r(peak_fc), "peak_actual": r(peak_act),
        "baseload_P10": r(p10), "baseload_P50": r(p50), "baseload_P90": r(p90),
    }


# ---------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------
def figure_pred_vs_actual(test, preds):
    """Daily-average predicted vs actual over the test window."""
    s = pd.DataFrame({"actual": test[TARGET].to_numpy(), "pred": np.asarray(preds)},
                     index=test.index).resample("1D").mean()
    plt.figure(figsize=(10, 4))
    plt.plot(s.index, s["actual"], color="black", label="Actual")
    plt.plot(s.index, s["pred"], color="#2980b9", label="Forecast (HGB)")
    plt.ylabel("Price (EUR/MWh)")
    plt.title("Test window: forecast vs actual (daily average)")
    plt.legend()
    plt.tight_layout()
    path = FIGURE_DIR / "forecast_vs_actual.png"
    plt.savefig(path, dpi=120)
    plt.close()
    return path


def figure_mae_by_hour(test, preds):
    """Mean absolute error by hour of day (where does the model struggle?)."""
    err = pd.DataFrame({"abs_err": np.abs(test[TARGET].to_numpy() - np.asarray(preds)),
                        "hour": test["hour"].to_numpy()})
    by_hour = err.groupby("hour")["abs_err"].mean()
    plt.figure(figsize=(10, 4))
    by_hour.plot(kind="bar", color="#16a085")
    plt.ylabel("MAE (EUR/MWh)")
    plt.xlabel("Hour of day (local)")
    plt.title("HGB error by hour of day (test window)")
    plt.tight_layout()
    path = FIGURE_DIR / "mae_by_hour.png"
    plt.savefig(path, dpi=120)
    plt.close()
    return path


# ---------------------------------------------------------------------------
# Report + main
# ---------------------------------------------------------------------------
def write_report(cv, test_metrics, curves, fig1, fig2):
    """Write outputs/forecast_metrics.md summarising the whole exercise."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    cv_summary = cv.groupby("model")[["MAE", "RMSE", "tail_MAE"]].mean().round(2)
    cv_summary = cv_summary.reindex(MODELS)

    lines = ["# Forecasting Report — Next-Day Day-Ahead Price (DE-LU)\n"]
    lines.append("Hourly DA price model. Features are leakage-free: wind+solar "
                 "day-ahead forecasts, calendar, and already-known price lags.\n")

    lines.append("## 1. Walk-forward cross-validation (mean over folds)\n")
    lines.append(cv_summary.to_markdown())
    lines.append(f"\n_{CV_FOLDS} expanding folds of {FOLD_DAYS} days each. "
                 "Model is selected on CV (HGB), not on the test set._\n")

    lines.append(f"## 2. Held-out test window (last {TEST_DAYS} days, out-of-sample)\n")
    lines.append(test_metrics.set_index("model").reindex(MODELS).to_markdown())
    lines.append("")

    lines.append("## 3. DA -> curve view (HGB forecast aggregated to blocks)\n")
    for label, curve in curves.items():
        lines.append(f"**{label}:**")
        for k, v in curve.items():
            lines.append(f"- {k}: {v}")
        lines.append("")
    lines.append("_Baseload P10/P50/P90 come from a block bootstrap of "
                 "out-of-sample daily forecast errors (not test residuals)._\n")

    lines.append("## 4. Figures\n")
    lines.append(f"- ![forecast vs actual]({fig1.relative_to(OUTPUT_DIR)})")
    lines.append(f"- ![mae by hour]({fig2.relative_to(OUTPUT_DIR)})")

    path = OUTPUT_DIR / "forecast_metrics.md"
    path.write_text("\n".join(lines))
    return path, cv_summary


def main():
    FIGURE_DIR.mkdir(parents=True, exist_ok=True)
    df = model_frame()
    print(f"Model-ready rows: {len(df):,}  ({df.index.min().date()} -> {df.index.max().date()})\n")

    print("Running walk-forward cross-validation...")
    cv, oos = cross_validate(df)

    print("Evaluating on the held-out test window...")
    test, preds_by_model, test_metrics = final_test(df)

    # The improved model (HGB) drives the submission and the curve view.
    hgb_preds = preds_by_model["hgb"]
    sub_path = write_submission(test, hgb_preds)

    # Curve view: aggregate the forecast to next-week and next-month blocks.
    daily_resid = daily_residuals(oos)
    curves = {
        "Next week (7d)": curve_view(test, hgb_preds, daily_resid, horizon_days=7),
        "Next month (30d)": curve_view(test, hgb_preds, daily_resid, horizon_days=30),
    }
    fig1 = figure_pred_vs_actual(test, hgb_preds)
    fig2 = figure_mae_by_hour(test, hgb_preds)
    report_path, cv_summary = write_report(cv, test_metrics, curves, fig1, fig2)

    print("\n=== Walk-forward CV (mean over folds) ===")
    print(cv_summary)
    print("\n=== Held-out test metrics ===")
    print(test_metrics.set_index("model").reindex(MODELS))
    print("\n=== Curve view ===")
    for label, curve in curves.items():
        print(f"  {label}: {curve}")
    print(f"\nReport -> {report_path}")
    print(f"Submission -> {sub_path}")


if __name__ == "__main__":
    main()

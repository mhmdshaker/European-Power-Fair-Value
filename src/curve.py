"""
curve.py
========
Translate the Day-Ahead price forecast into a TRADABLE prompt-curve view.

We do not need forward price data to show the linkage. The chain is:

  hourly DA forecast  ->  delivery-period FAIR VALUE (base + peak, with bands)
                      ->  EDGE vs a market anchor
                      ->  confidence-weighted SIGNAL (level + shape)
                      ->  desk expression + INVALIDATION rules

Market anchor (proxy for the prompt curve): because we use no paid forward data,
we anchor to the *trailing 30-day realised average* — a transparent stand-in for
where a persistence-minded market might mark the prompt. In production you would
drop in the live forward quote instead; the signal maths is identical.

Run with:   python -m src.curve
"""

import matplotlib
matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from config import FIGURE_DIR, OUTPUT_DIR, PROCESSED_DIR
from src import forecast as fc
from src.features import TARGET, model_frame

# --- Desk / trading parameters ---------------------------------------------
MAX_CLIP_MW = 100        # largest position the desk would express on this signal
ANCHOR_LOOKBACK_DAYS = 30
DRIFT_TOLERANCE_PCT = 15  # renewables forecast vs realised tolerance before FV is stale


# ---------------------------------------------------------------------------
# Market anchor (proxy for the forward / prompt mark)
# ---------------------------------------------------------------------------
def market_anchor(prices, delivery_start, lookback_days=ANCHOR_LOOKBACK_DAYS):
    """Trailing realised average price just before the delivery period."""
    window = prices[(prices.index < delivery_start)
                    & (prices.index >= delivery_start - pd.Timedelta(days=lookback_days))]
    return float(window.mean())


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------
def level_signal(fv, anchor):
    """Baseload level view: our fair value vs the market anchor, sized by confidence."""
    p10, p50, p90 = fv["baseload_P10"], fv["baseload_P50"], fv["baseload_P90"]
    band = (p90 - p10) / 2            # half-width of the forecast distribution
    edge = p50 - anchor              # +ve => we think the curve is cheap (go long)
    z = edge / band if band else 0.0

    if abs(edge) < band:
        # Edge sits inside our own forecast noise -> no conviction.
        direction, position, note = "FLAT", 0.0, "edge within forecast band -> stand aside"
    else:
        direction = "LONG" if edge > 0 else "SHORT"
        # Confidence-weighted: scale by z, cap at +/-2 sigma, clip to max size.
        position = float(np.clip(z, -2, 2) / 2 * MAX_CLIP_MW)
        note = "tradable edge beyond forecast band"

    return {"anchor": round(anchor, 2), "fair_value_P50": p50,
            "edge_eur_mwh": round(edge, 2), "band_halfwidth": round(band, 2),
            "z_score": round(z, 2), "direction": direction,
            "position_mw": round(position, 1), "note": note}


def shape_signal(fv, base_anchor, peak_anchor):
    """Peak-vs-base shape view (a spread trade, independent of the level)."""
    fv_spread = fv["peak_forecast"] - fv["baseload_forecast"]
    anchor_spread = peak_anchor - base_anchor
    edge = fv_spread - anchor_spread
    direction = ("LONG peak/base spread" if edge > 0
                 else "SHORT peak/base spread" if edge < 0 else "FLAT")
    return {"fv_peak_base_spread": round(fv_spread, 2),
            "anchor_peak_base_spread": round(anchor_spread, 2),
            "spread_edge_eur_mwh": round(edge, 2), "direction": direction}


# ---------------------------------------------------------------------------
# Invalidation checks (when to drop the trade)
# ---------------------------------------------------------------------------
def invalidation_checks(test, fv):
    """Concrete, computable conditions that would invalidate the signal."""
    checks = []

    # 1. Did the realised period average stay inside our P10-P90 band?
    actual = fv["baseload_actual"]
    inside = fv["baseload_P10"] <= actual <= fv["baseload_P90"]
    checks.append(("Realised avg within P10-P90 band", inside,
                   f"actual {actual} vs [{fv['baseload_P10']}, {fv['baseload_P90']}]"))

    # 2. Did realised renewables track the forecast the FV was built on?
    proc = pd.read_parquet(PROCESSED_DIR / "dataset.parquet")
    proc = proc.loc[proc.index.isin(test.index)]
    fcst_renew = (test["wind_fcst_mw"] + test["solar_fcst_mw"]).mean()
    act_renew = (proc["wind_mw"] + proc["solar_mw"]).mean()
    drift = 100 * (act_renew - fcst_renew) / fcst_renew
    ok = abs(drift) <= DRIFT_TOLERANCE_PCT
    checks.append((f"Renewable forecast drift <= {DRIFT_TOLERANCE_PCT}%", ok,
                   f"realised vs forecast renewables drift {drift:+.1f}%"))

    return checks


# ---------------------------------------------------------------------------
# Figure
# ---------------------------------------------------------------------------
def figure_curve_view(fv, anchor, fig_path):
    """Compare market anchor, our fair-value band, and the realised average."""
    plt.figure(figsize=(7, 4.5))
    # Fair value as a point with a P10-P90 error bar.
    plt.errorbar([1], [fv["baseload_P50"]],
                 yerr=[[fv["baseload_P50"] - fv["baseload_P10"]],
                       [fv["baseload_P90"] - fv["baseload_P50"]]],
                 fmt="o", color="#2980b9", capsize=8, markersize=9,
                 label="Fair value (P50, P10-P90)")
    plt.scatter([0], [anchor], color="black", s=80, label="Market anchor (trailing 30d)")
    plt.scatter([2], [fv["baseload_actual"]], color="#c0392b", s=80, marker="s",
                label="Realised average")
    plt.xticks([0, 1, 2], ["Anchor", "Fair value", "Actual"])
    plt.ylabel("Baseload price (EUR/MWh)")
    plt.title("Prompt-month baseload: fair value vs anchor vs actual")
    plt.legend()
    plt.grid(axis="y", alpha=0.3)
    plt.tight_layout()
    plt.savefig(fig_path, dpi=120)
    plt.close()


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
DESK_PLAYBOOK = """\
## What the desk does with this

- **Level (baseload):** a LONG signal means our fair value sits above the market
  anchor, i.e. the prompt looks cheap — express by buying the **prompt-month
  baseload** (size = `position_mw`, confidence-weighted). SHORT = sell it.
- **Shape (peak/base):** trade the **peak vs off-peak spread** when our forecast
  shape differs from the anchor's. In summer DE, midday solar can push peak below
  base — a structural shape view, separable from the level.
- **Scaling up the curve:** the same hourly model, aggregated over a quarter,
  gives a **prompt-quarter** view; differences between months drive **calendar
  spreads**.
- **Sizing:** position is proportional to the edge in units of forecast standard
  deviation (`z_score`), capped at the desk clip. Edges inside the forecast band
  are not traded.

## What invalidates the signal

1. **Band breach** — realised period average leaves the P10-P90 band: the model is
   mis-calibrated for the current regime; stand down and recalibrate.
2. **Fundamentals drift** — realised wind/solar diverge from the forecast the fair
   value was built on (> tolerance): the FV is stale; re-run with fresh forecasts.
3. **Edge within noise** — `|edge| < band half-width`: no tradable conviction.
4. **Regime shocks (manual gate)** — large gas/carbon moves change marginal cost
   and are not yet in the model; flag and re-assess before trading.
"""


def write_report(fv, level, shape, checks, fig_path):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    lines = ["# Prompt-Curve Translation — DE-LU (prompt month)\n"]
    lines.append("Turns the Day-Ahead price forecast into a tradable curve view. "
                 "Market anchor = trailing 30-day realised average (proxy for the "
                 "prompt forward; swap in a live quote in production).\n")

    lines.append("## 1. Delivery-period fair value (next month, baseload + peak)\n")
    for k in ["baseload_forecast", "baseload_P10", "baseload_P50", "baseload_P90",
              "peak_forecast", "baseload_actual", "peak_actual"]:
        lines.append(f"- {k}: {fv[k]}")
    lines.append("")

    lines.append("## 2. Level signal (baseload)\n")
    for k, v in level.items():
        lines.append(f"- {k}: {v}")
    lines.append("")

    lines.append("## 3. Shape signal (peak vs base)\n")
    for k, v in shape.items():
        lines.append(f"- {k}: {v}")
    lines.append("")

    lines.append("## 4. Invalidation checks\n")
    for name, ok, detail in checks:
        lines.append(f"- {'PASS' if ok else 'FAIL'} — {name} ({detail})")
    lines.append("")

    lines.append(DESK_PLAYBOOK)
    lines.append(f"\n## Figure\n\n- ![curve view]({fig_path.relative_to(OUTPUT_DIR)})")

    path = OUTPUT_DIR / "curve_view.md"
    path.write_text("\n".join(lines))
    return path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    FIGURE_DIR.mkdir(parents=True, exist_ok=True)
    df = model_frame()

    # Reuse the Task-2 model: out-of-sample residuals + test-window forecast.
    print("Fitting model (walk-forward) and forecasting the delivery period...")
    _, oos = fc.cross_validate(df)
    test, preds_by_model, _ = fc.final_test(df)
    hgb = preds_by_model["hgb"]
    daily_resid = fc.daily_residuals(oos)

    # Fair value for the prompt month (the held-out test window is our delivery period).
    fv = fc.curve_view(test, hgb, daily_resid, horizon_days=30)

    # Market anchors from realised prices just before the delivery period.
    delivery_start = test.index.min()
    prices = df[TARGET]
    base_anchor = market_anchor(prices, delivery_start)
    peak_anchor = market_anchor(prices[df["is_peak"] == 1], delivery_start)

    level = level_signal(fv, base_anchor)
    shape = shape_signal(fv, base_anchor, peak_anchor)
    checks = invalidation_checks(test, fv)

    fig_path = FIGURE_DIR / "curve_view.png"
    figure_curve_view(fv, base_anchor, fig_path)
    report_path = write_report(fv, level, shape, checks, fig_path)

    print("\n=== Level signal (baseload) ===")
    for k, v in level.items():
        print(f"  {k}: {v}")
    print("\n=== Shape signal (peak vs base) ===")
    for k, v in shape.items():
        print(f"  {k}: {v}")
    print("\n=== Invalidation checks ===")
    for name, ok, detail in checks:
        print(f"  {'PASS' if ok else 'FAIL'} - {name} ({detail})")
    print(f"\nReport -> {report_path}")


if __name__ == "__main__":
    main()

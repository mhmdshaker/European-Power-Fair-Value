"""
qa.py
=====
Data-quality checks on the merged hourly dataset, plus a few figures.

What it reports:
  * Coverage    - how many hours we have vs. how many we expect
  * Missingness - % of NaN per column
  * Duplicates  - repeated timestamps (should be zero after the transform)
  * Outliers    - negative prices, price spikes, negative generation, etc.
  * DST sanity  - spring-forward should give a 23-hour day, fall-back 25 hours

Outputs:
  * outputs/qa_report.md          (human-readable summary with tables)
  * outputs/figures/*.png         (at least two figures)

Run from the project root with:   python -m src.qa
"""

import matplotlib
matplotlib.use("Agg")  # render to files, no interactive window needed

import matplotlib.pyplot as plt
import pandas as pd

from config import FIGURE_DIR, OUTPUT_DIR, PROCESSED_DIR, TIMEZONE

# Columns we expect after the transform.
VALUE_COLS = ["price_eur_mwh", "load_mw", "wind_mw", "solar_mw", "net_flow_mw"]


def load_dataset():
    """Read the processed hourly dataset (UTC index + local-time column)."""
    return pd.read_parquet(PROCESSED_DIR / "dataset.parquet")


# ---------------------------------------------------------------------------
# Individual checks  (each returns a small DataFrame / dict for the report)
# ---------------------------------------------------------------------------
def check_coverage(df):
    """Compare actual row count to the number of hours in the full span."""
    span_hours = int((df.index.max() - df.index.min()).total_seconds() // 3600) + 1
    return {
        "first_timestamp_utc": df.index.min(),
        "last_timestamp_utc": df.index.max(),
        "rows_present": len(df),
        "hours_expected": span_hours,
        "coverage_pct": round(100 * len(df) / span_hours, 2),
    }


def check_missingness(df):
    """Count and percentage of missing values per column."""
    miss = df[VALUE_COLS].isna().sum()
    pct = (100 * miss / len(df)).round(2)
    return pd.DataFrame({"missing": miss, "missing_pct": pct})


def check_duplicates(df):
    """Number of duplicated timestamps in the index (should be 0)."""
    return int(df.index.duplicated().sum())


def check_outliers(df):
    """Flag values that look suspicious (we flag, we do NOT silently drop)."""
    price = df["price_eur_mwh"]
    rows = {
        # Negative prices are REAL in Germany (oversupply) -> flag, keep them.
        "negative_prices": int((price < 0).sum()),
        "price_above_500": int((price > 500).sum()),
        "price_min": round(price.min(), 2),
        "price_max": round(price.max(), 2),
        # Generation should never be negative.
        "negative_wind": int((df["wind_mw"] < 0).sum()),
        "negative_solar": int((df["solar_mw"] < 0).sum()),
        # Sanity bounds on load (Germany ~ 30-90 GW).
        "load_min_mw": round(df["load_mw"].min(), 0),
        "load_max_mw": round(df["load_mw"].max(), 0),
    }
    return rows


def check_dst(df):
    """Find days that are not 24 hours long: spring=23h, fall=25h.

    Because we join on UTC but display in local time, a correct dataset will
    show exactly 23 hours on the spring-forward date and 25 on fall-back.
    """
    local_date = df["timestamp_local"].dt.date
    hours_per_day = df.groupby(local_date).size()
    odd_days = hours_per_day[hours_per_day != 24]
    return odd_days


# ---------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------
def figure_missingness(df):
    """Bar chart: % missing per column."""
    pct = (100 * df[VALUE_COLS].isna().sum() / len(df))
    ax = pct.plot(kind="bar", color="#c0392b")
    ax.set_ylabel("% missing")
    ax.set_title("Missing values by field")
    plt.tight_layout()
    path = FIGURE_DIR / "missingness.png"
    plt.savefig(path, dpi=120)
    plt.close()
    return path


def figure_price_and_renewables(df):
    """Overview: daily-average price vs daily-average wind+solar."""
    daily = df.resample("1D").mean(numeric_only=True)
    fig, ax1 = plt.subplots(figsize=(10, 4))
    ax1.plot(daily.index, daily["price_eur_mwh"], color="black", label="Price")
    ax1.set_ylabel("Price (EUR/MWh)")
    ax2 = ax1.twinx()
    ax2.plot(daily.index, daily["wind_mw"] + daily["solar_mw"],
             color="#2980b9", alpha=0.6, label="Wind+Solar")
    ax2.set_ylabel("Wind + Solar (MW)")
    ax1.set_title("Daily average: price vs. renewable generation")
    plt.tight_layout()
    path = FIGURE_DIR / "price_vs_renewables.png"
    plt.savefig(path, dpi=120)
    plt.close()
    return path


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------
def write_report(df):
    """Run every check, save figures, and write outputs/qa_report.md."""
    FIGURE_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    coverage = check_coverage(df)
    missing = check_missingness(df)
    duplicates = check_duplicates(df)
    outliers = check_outliers(df)
    dst = check_dst(df)

    fig1 = figure_missingness(df)
    fig2 = figure_price_and_renewables(df)

    lines = []
    lines.append("# Data Quality Report — German Power (DE-LU)\n")
    lines.append(f"Source: Energy-Charts API (Fraunhofer ISE). "
                 f"Generated automatically by `src/qa.py`.\n")

    lines.append("## 1. Coverage\n")
    for k, v in coverage.items():
        lines.append(f"- **{k}**: {v}")
    lines.append("")

    lines.append("## 2. Missingness by field\n")
    lines.append(missing.to_markdown())
    lines.append("")

    lines.append("## 3. Duplicates\n")
    lines.append(f"- Duplicated timestamps: **{duplicates}**\n")

    lines.append("## 4. Outliers & sanity bounds\n")
    for k, v in outliers.items():
        lines.append(f"- **{k}**: {v}")
    lines.append("")

    lines.append("## 5. Daylight-saving-time check\n")
    lines.append("Days that are not 24 hours long (expected: 23h in spring, "
                 "25h in autumn each year):\n")
    lines.append(dst.to_frame("hours_in_day").to_markdown())
    lines.append("")

    lines.append("## 6. Figures\n")
    lines.append(f"- ![missingness]({fig1.relative_to(OUTPUT_DIR)})")
    lines.append(f"- ![price vs renewables]({fig2.relative_to(OUTPUT_DIR)})")
    lines.append("")

    report_path = OUTPUT_DIR / "qa_report.md"
    report_path.write_text("\n".join(lines))

    # Print a short console summary too.
    print(f"Coverage: {coverage['coverage_pct']}%  "
          f"({coverage['rows_present']:,}/{coverage['hours_expected']:,} hours)")
    print(f"Duplicates: {duplicates}")
    print(f"Negative prices: {outliers['negative_prices']}  |  "
          f"Price range: {outliers['price_min']} to {outliers['price_max']} EUR/MWh")
    print(f"Odd-length (DST) days found: {len(dst)}")
    print(f"Report -> {report_path}")
    print(f"Figures -> {fig1}, {fig2}")


if __name__ == "__main__":
    write_report(load_dataset())

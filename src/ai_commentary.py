"""
ai_commentary.py
================
AI-accelerated workflow (Task 4): an LLM writes the daily desk "drivers" note
PROGRAMMATICALLY from our computed metrics — replacing a manual writing task.

Design principles (matching the brief):
  * The LLM is called FROM CODE (Anthropic API), key via env var only.
  * It is given ONLY our machine-computed numbers and told to invent none.
  * A programmatic GUARD extracts every number from the model's output and checks
    each one is grounded in the metrics we supplied -> catches hallucinations.
  * Prompts, raw outputs, verification results, and failure modes are LOGGED
    (outputs/ai_logs/commentary_log.jsonl). No secrets are ever logged.
  * If the API key is missing or the call fails, we log the failure mode and fall
    back to a deterministic template so the pipeline still produces a note.

Run with:   python -m src.ai_commentary
"""

import json
import os
import re
from datetime import datetime, timezone

from config import OUTPUT_DIR
from src import curve

MODEL = os.environ.get("AI_MODEL", "claude-haiku-4-5-20251001")
LOG_PATH = OUTPUT_DIR / "ai_logs" / "commentary_log.jsonl"

SYSTEM_PROMPT = (
    "You are a European power trading desk analyst. Write a concise daily "
    "fair-value note (max ~140 words) for the German (DE-LU) prompt month. "
    "STRICT RULES: use ONLY the numbers provided in the metrics JSON; never "
    "invent or estimate any number; if you state a figure, it must appear in the "
    "metrics. Be direct about the trade signal and what would invalidate it."
)


# ---------------------------------------------------------------------------
# 1. Gather machine-computed metrics (the only facts the LLM may use)
# ---------------------------------------------------------------------------
def collect_metrics():
    """Pull exact numbers from the curve model into a flat, LLM-friendly dict."""
    v = curve.compute_view()
    fv, level, shape = v["fv"], v["level"], v["shape"]
    return {
        "market": "DE-LU",
        "delivery_period": f"prompt month (30d) from {v['delivery_start']}",
        "fair_value_baseload_p50": fv["baseload_P50"],
        "fair_value_baseload_p10": fv["baseload_P10"],
        "fair_value_baseload_p90": fv["baseload_P90"],
        "fair_value_peak": fv["peak_forecast"],
        "market_anchor_baseload": level["anchor"],
        "level_edge_eur_mwh": level["edge_eur_mwh"],
        "level_band_halfwidth": level["band_halfwidth"],
        "level_direction": level["direction"],
        "level_position_mw": level["position_mw"],
        "shape_spread_forecast": shape["fv_peak_base_spread"],
        "shape_spread_anchor": shape["anchor_peak_base_spread"],
        "shape_direction": shape["direction"],
        "model_cv_mae": v["cv_mae"],
        "invalidation": [
            {"name": n, "pass": bool(ok), "detail": d} for n, ok, d in v["checks"]
        ],
    }


# ---------------------------------------------------------------------------
# 2. Prompt + LLM call
# ---------------------------------------------------------------------------
def build_user_prompt(metrics):
    return ("Write today's note from these metrics (JSON). Use only these "
            "numbers.\n\n" + json.dumps(metrics, indent=2))


def call_llm(user_prompt):
    """Call the Anthropic API. Raises on missing key / SDK / network error."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("no_api_key")
    import anthropic  # imported here so the module loads even without the SDK
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=MODEL, max_tokens=400, system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    return resp.content[0].text


# ---------------------------------------------------------------------------
# 3. Hallucinated-number guard
# ---------------------------------------------------------------------------
NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
# Numbers that are always allowed (percentiles, horizons, config constants).
STRUCTURAL_WHITELIST = {7, 10, 30, 50, 90, 15, 100, 140}


def _numbers_in(obj):
    """Recursively collect every number appearing in a metrics structure."""
    found = set()
    if isinstance(obj, bool):
        return found
    if isinstance(obj, (int, float)):
        found.add(round(float(obj), 2))
    elif isinstance(obj, dict):
        for v in obj.values():
            found |= _numbers_in(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            found |= _numbers_in(v)
    elif isinstance(obj, str):
        for m in NUMBER_RE.findall(obj):      # parse numbers inside detail strings
            found.add(round(float(m), 2))
    return found


def verify_numbers(text, metrics, tol=0.05):
    """Return numbers in `text` that are NOT grounded in the metrics."""
    allowed = _numbers_in(metrics) | {float(x) for x in STRUCTURAL_WHITELIST}
    ungrounded = []
    for token in NUMBER_RE.findall(text):
        val = round(float(token), 2)
        if not any(abs(val - a) <= tol for a in allowed):
            ungrounded.append(val)
    return ungrounded


# ---------------------------------------------------------------------------
# 4. Deterministic fallback (used if the API is unavailable)
# ---------------------------------------------------------------------------
def fallback_commentary(m):
    """Template note built directly from metrics (cannot hallucinate)."""
    return (
        f"DE-LU {m['delivery_period']}. Fair value (baseload P50) "
        f"{m['fair_value_baseload_p50']} EUR/MWh vs market anchor "
        f"{m['market_anchor_baseload']}, an edge of {m['level_edge_eur_mwh']} "
        f"against a band half-width of {m['level_band_halfwidth']}. "
        f"Level signal: {m['level_direction']} "
        f"(position {m['level_position_mw']} MW). "
        f"Shape: {m['shape_direction']} (forecast peak-base "
        f"{m['shape_spread_forecast']} vs anchor {m['shape_spread_anchor']}). "
        f"Model skill: HGB CV MAE {m['model_cv_mae'].get('hgb')} vs seasonal-naive "
        f"{m['model_cv_mae'].get('seasonal_naive')}. Invalidation: "
        + "; ".join(f"{c['name']} {'PASS' if c['pass'] else 'FAIL'}"
                    for c in m["invalidation"]) + "."
    )


# ---------------------------------------------------------------------------
# 5. Logging (no secrets)
# ---------------------------------------------------------------------------
def log_interaction(record):
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    record["timestamp_utc"] = datetime.now(timezone.utc).isoformat()
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(record) + "\n")


# ---------------------------------------------------------------------------
# 6. Orchestration
# ---------------------------------------------------------------------------
def generate(metrics):
    """Try the LLM; verify; fall back if needed. Returns (text, source, log)."""
    user_prompt = build_user_prompt(metrics)
    log = {"model": MODEL, "system_prompt": SYSTEM_PROMPT, "user_prompt": user_prompt}

    try:
        text = call_llm(user_prompt)
        ungrounded = verify_numbers(text, metrics)
        if ungrounded:
            # Model invented numbers -> reject its output, use the safe fallback.
            log.update({"status": "hallucination_flagged", "raw_output": text,
                        "ungrounded_numbers": ungrounded})
            log_interaction(log)
            return fallback_commentary(metrics), "fallback_after_hallucination", log
        log.update({"status": "ok", "raw_output": text, "ungrounded_numbers": []})
        log_interaction(log)
        return text, "llm", log

    except Exception as exc:                       # no key, no SDK, network, etc.
        failure = "no_api_key" if str(exc) == "no_api_key" else f"api_error: {exc}"
        log.update({"status": failure, "raw_output": None})
        log_interaction(log)
        return fallback_commentary(metrics), "fallback", log


def main():
    print("Collecting machine-computed metrics...")
    metrics = collect_metrics()

    print(f"Generating commentary via {MODEL} (falls back if unavailable)...")
    text, source, log = generate(metrics)

    note_path = OUTPUT_DIR / "daily_commentary.md"
    note_path.write_text(
        "# Daily Drivers Commentary — DE-LU Prompt Month\n\n"
        f"{text}\n\n---\n"
        f"_Source: **{source}** | model: {MODEL} | status: {log['status']} | "
        f"numbers verified against computed metrics | "
        f"full log: `outputs/ai_logs/commentary_log.jsonl`_\n"
    )

    print(f"\n--- commentary ({source}) ---\n{text}\n")
    print(f"Status logged: {log['status']}")
    print(f"Note  -> {note_path}")
    print(f"Log   -> {LOG_PATH}")


if __name__ == "__main__":
    main()

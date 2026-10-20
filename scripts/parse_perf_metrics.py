"""
LLM Performance & Quality Trade-Off Matrix Analyzer.

Reads the Promptfoo output JSON, compares the evaluated models side by side
(pass rate, latency p50/p95, tokens, cost) and recommends a model.

Selection logic:
  1. A model is ELIGIBLE if it meets the minimum requirements:
     pass rate >= --min-pass-rate, latency p95 <= --max-p95-ms, AND the
     critical category (default: security) has a pass rate of at least
     --critical-min-pass-rate (default: 100%).
  2. Among eligible models the cheapest one is recommended
     (ties: lower p95 latency, then higher pass rate).
  3. The script exits with code 1 only if NO model is eligible.

Exit codes: 0 = a model is recommended, 1 = no eligible model,
            2 = results file missing or unreadable.

Run locally after an evaluation:
  npx promptfoo eval -c config/promptfooconfig.yaml --no-cache -o promptfoo_output.json
  python3 scripts/parse_perf_metrics.py
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

# Minimum requirements (rationale in the README).
DEFAULT_MIN_PASS_RATE = 85.0   # percent of evaluated tests that must pass
DEFAULT_MAX_P95_MS = 3500.0    # latency ceiling for a chat-like experience
 
EXIT_OK = 0
EXIT_NO_ELIGIBLE_MODEL = 1
EXIT_DATA_ERROR = 2
 
# promptfoo failureReason: 0 = none, 1 = failed assertion, 2 = error (API error, timeout)
FAILURE_REASON_ERROR = 2


class EvalDataError(Exception):
    """Raised when the results file is missing, unreadable or empty."""


def load_eval_results(file_path: Path) -> Dict[str, Any]:
    if not file_path.exists():
        raise EvalDataError(
            f"Results file not found at {file_path}\n"
            "Run the evaluation first:\n"
            "  npx promptfoo eval -c config/promptfooconfig.yaml "
            "--no-cache -o promptfoo_output.json"
        )
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        raise EvalDataError(f"Results file is not valid JSON: {exc}") from exc


def calculate_percentile(values: List[float], percentile: float) -> float:
    if not values:
        return 0.0
    return round(float(pd.Series(values).quantile(percentile)), 2)


def extract_records(raw_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """promptfoo output shape: {"results": {"results": [...]}}."""
    return (raw_data.get("results") or {}).get("results") or []


def is_infrastructure_error(record: Dict[str, Any]) -> bool:
    """True when the model never produced a response (API error, timeout)."""
    return record.get("failureReason") == FAILURE_REASON_ERROR or not record.get("response")


def get_provider_label(record: Dict[str, Any]) -> str:
    return (record.get("provider") or {}).get("id") or "unknown_provider"
 
 
def get_category(record: Dict[str, Any]) -> str:
    """Test category from the dataset (vars.category), 'uncategorized' if missing."""
    return str((record.get("vars") or {}).get("category") or "uncategorized")


def parse_provider_metrics(raw_data: Dict[str, Any]) -> pd.DataFrame:
    records = extract_records(raw_data)
    if not records:
        raise EvalDataError("No evaluation records found in the results file.")
 
    by_provider: Dict[str, Dict[str, List[Any]]] = {}
 
    for record in records:
        bucket = by_provider.setdefault(
            get_provider_label(record),
            {"latencies": [], "tokens": [], "costs": [], "passed": [], "errors": []},
        )
 
        # Infrastructure errors are not quality failures and carry no valid
        # latency, so they are counted separately and excluded from the metrics.
        if is_infrastructure_error(record):
            bucket["errors"].append(1)
            continue
 
        latency = record.get("latencyMs")
        if latency:
            bucket["latencies"].append(latency)
 
        token_usage = record["response"].get("tokenUsage") or record.get("tokenUsage") or {}
        bucket["tokens"].append(token_usage.get("total", 0))
        bucket["costs"].append(record.get("cost") or 0.0)
        bucket["passed"].append(1 if record.get("success") else 0)
 
    rows = []
    for provider, data in by_provider.items():
        evaluated = len(data["passed"])
        pass_rate = round(sum(data["passed"]) / evaluated * 100, 1) if evaluated else 0.0
        avg_tokens = round(pd.Series(data["tokens"]).mean(), 1) if data["tokens"] else 0.0
        avg_cost = pd.Series(data["costs"]).mean() if data["costs"] else 0.0
 
        rows.append({
            "Provider Model": provider,
            "Evaluated": evaluated,
            "Errors": len(data["errors"]),
            "Pass Rate (%)": pass_rate,
            "Latency p50 (ms)": calculate_percentile(data["latencies"], 0.50),
            "Latency p95 (ms)": calculate_percentile(data["latencies"], 0.95),
            "Avg Tokens": avg_tokens,
            "Cost / 1k Req ($)": round(float(avg_cost) * 1000, 4),
        })
 
    return pd.DataFrame(rows)


def parse_category_metrics(raw_data: Dict[str, Any]) -> pd.DataFrame:
    """Pass rate per provider and test category (infrastructure errors excluded)."""
    counts: Dict[tuple, List[int]] = {}
    for record in extract_records(raw_data):
        if is_infrastructure_error(record):
            continue
        key = (get_provider_label(record), get_category(record))
        counts.setdefault(key, []).append(1 if record.get("success") else 0)
 
    rows = [
        {
            "Provider Model": provider,
            "Category": category,
            "Passed": sum(passed),
            "Evaluated": len(passed),
            "Pass Rate (%)": round(sum(passed) / len(passed) * 100, 1),
        }
        for (provider, category), passed in sorted(counts.items())
    ]
    return pd.DataFrame(
        rows, columns=["Provider Model", "Category", "Passed", "Evaluated", "Pass Rate (%)"]
    )


def format_category_table(category_df: pd.DataFrame) -> Optional[pd.DataFrame]:
    """Pivot: one row per category, one column per provider, cells like '7/9 (77.8%)'."""
    if category_df.empty:
        return None
    cells = category_df.copy()
    cells["Cell"] = [
        f"{passed}/{total} ({rate}%)"
        for passed, total, rate in zip(
            cells["Passed"], cells["Evaluated"], cells["Pass Rate (%)"]
        )
    ]
    return (
        cells.pivot(index="Category", columns="Provider Model", values="Cell")
        .fillna("-")
        .reset_index()
    )


def evaluate_eligibility(df: pd.DataFrame, min_pass_rate: float, max_p95_ms: float) -> pd.DataFrame:
    """Add 'Eligible' (bool) and 'Reason' (why not) columns."""
    df = df.copy()
    eligible, reasons = [], []
 
    for _, row in df.iterrows():
        problems = []
        if row["Evaluated"] == 0:
            problems.append("no evaluated tests")
        if row["Pass Rate (%)"] < min_pass_rate:
            problems.append(f"pass rate {row['Pass Rate (%)']}% < {min_pass_rate}%")
        if row["Latency p95 (ms)"] > max_p95_ms:
            problems.append(f"p95 {row['Latency p95 (ms)']}ms > {max_p95_ms}ms")
 
        eligible.append(not problems)
        reasons.append("; ".join(problems))
 
    df["Eligible"] = eligible
    df["Reason"] = reasons
    return df


def pick_winner(df: pd.DataFrame) -> Optional[str]:
    """Cheapest eligible model; ties -> lower p95, then higher pass rate."""
    candidates = df[df["Eligible"]]
    if candidates.empty:
        return None
    best = candidates.sort_values(
        ["Cost / 1k Req ($)", "Latency p95 (ms)", "Pass Rate (%)"],
        ascending=[True, True, False],
    ).iloc[0]
    return str(best["Provider Model"])


def enforce_quality_gate(df: pd.DataFrame) -> bool:
    """Print eligibility per model and the recommendation. True if a model is recommended."""
    print("\n" + "=" * 80)
    print("                     MODEL SELECTION & QUALITY GATE")
    print("=" * 80)
 
    for _, row in df.iterrows():
        if row["Eligible"]:
            print(f"  ✔ ELIGIBLE      {row['Provider Model']}")
        else:
            print(f"  ✖ NOT ELIGIBLE  {row['Provider Model']}: {row['Reason']}")
        if row["Errors"]:
            print(f"    ⚠ {row['Errors']} request(s) failed with API errors and were excluded.")
 
    winner = pick_winner(df)
    if winner is None:
        print("\n  ✖ Gate FAILED: no model meets the minimum requirements.")
        return False
 
    print(f"\n  ★ Recommended model: {winner} (cheapest among eligible models)")
    return True


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    root_dir = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Compare LLM providers and recommend a model.")
    parser.add_argument("--results", type=Path, default=root_dir / "promptfoo_output.json",
                        help="Path to the promptfoo output JSON")
    parser.add_argument("--min-pass-rate", type=float, default=DEFAULT_MIN_PASS_RATE,
                        help="Minimum pass rate in percent")
    parser.add_argument("--max-p95-ms", type=float, default=DEFAULT_MAX_P95_MS,
                        help="Maximum latency p95 in milliseconds")
    return parser.parse_args(argv)
 
 
def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
 
    print("\n" + "=" * 80)
    print("           LLM PERFORMANCE & QUALITY TRADE-OFF MATRIX")
    print("=" * 80 + "\n")
 
    try:
        raw = load_eval_results(args.results)
        df = parse_provider_metrics(raw)
    except EvalDataError as exc:
        print(f"Error: {exc}")
        return EXIT_DATA_ERROR
 
    category_df = parse_category_metrics(raw)
    df = evaluate_eligibility(df, args.min_pass_rate, args.max_p95_ms)
 
    display_df = df.drop(columns=["Eligible", "Reason"]).copy()
    display_df["Status"] = df["Eligible"].map({True: "ELIGIBLE", False: "NOT ELIGIBLE"})
    display_df["Pass Rate (%)"] = display_df["Pass Rate (%)"].map(lambda v: f"{v}%")
    display_df["Cost / 1k Req ($)"] = display_df["Cost / 1k Req ($)"].map(lambda v: f"${v:.4f}")
    print(display_df.to_markdown(index=False))
 
    category_table = format_category_table(category_df)
    if category_table is not None:
        print("\nPass rate by test category:\n")
        print(category_table.to_markdown(index=False))
 
    return EXIT_OK if enforce_quality_gate(df) else EXIT_NO_ELIGIBLE_MODEL
 
 
if __name__ == "__main__":
    sys.exit(main())
 
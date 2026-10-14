"""
LLM Performance & Quality Trade-Off Matrix Analyzer.
Parses Promptfoo output JSON, displays performance benchmarks,
and enforces CI/CD Quality Gate SLAs (Latency p95 and Pass Rate).
"""

import json
import sys
from pathlib import Path
from typing import Any, Dict, List
import pandas as pd

# SLA Thresholds for CI/CD Quality Gate
MAX_P95_LATENCY_MS = 3500.0  # Production latency ceiling
MIN_PASS_RATE_PERCENT = 50.0  # Minimum acceptable quality pass rate


def load_eval_results(file_path: Path) -> Dict[str, Any]:
    if not file_path.exists():
        print(f"Error: Results file not found at {file_path}")
        print("Run evaluation first: npx promptfoo eval -c config/promptfooconfig.yaml -o promptfoo_output.json")
        sys.exit(1)

    with open(file_path, "r", encoding="utf-8") as f:
        return json.load(f)


def calculate_percentile(values: List[float], percentile: float) -> float:
    if not values:
        return 0.0
    series = pd.Series(values)
    return round(float(series.quantile(percentile)), 2)


def parse_provider_metrics(raw_data: Dict[str, Any]) -> pd.DataFrame:
    raw_results = raw_data.get("results", {})
    records = []

    if isinstance(raw_results, dict) and "results" in raw_results:
        records = raw_results.get("results", [])
    elif isinstance(raw_results, list):
        records = raw_results

    if not records:
        print("Error: No evaluation records found in promptfoo_output.json.")
        sys.exit(1)

    metrics_by_provider: Dict[str, Dict[str, List[Any]]] = {}

    for record in records:
        provider_obj = record.get("provider", {})
        provider_label = provider_obj.get("id") if isinstance(provider_obj, dict) else str(provider_obj)
        if not provider_label:
            provider_label = "unknown_provider"

        if provider_label not in metrics_by_provider:
            metrics_by_provider[provider_label] = {
                "latencies": [],
                "tokens": [],
                "costs": [],
                "passed": []
            }

        latency = record.get("latencyMs", 0)
        cost = record.get("cost", 0.0)
        is_pass = record.get("success", False)

        token_usage = record.get("response", {}).get("tokenUsage") or record.get("tokenUsage", {})
        total_tokens = token_usage.get("total", 0) if isinstance(token_usage, dict) else 0

        metrics_by_provider[provider_label]["latencies"].append(latency)
        metrics_by_provider[provider_label]["costs"].append(cost)
        metrics_by_provider[provider_label]["passed"].append(1 if is_pass else 0)
        metrics_by_provider[provider_label]["tokens"].append(total_tokens)

    summary_records = []
    for provider, data in metrics_by_provider.items():
        total_evals = len(data["passed"])
        pass_count = sum(data["passed"])
        pass_rate = round((pass_count / total_evals) * 100, 1) if total_evals > 0 else 0.0

        p50_latency = calculate_percentile(data["latencies"], 0.50)
        p95_latency = calculate_percentile(data["latencies"], 0.95)

        avg_tokens = round(pd.Series(data["tokens"]).mean(), 1) if data["tokens"] else 0.0
        avg_cost_per_req = pd.Series(data["costs"]).mean() if data["costs"] else 0.0
        cost_per_1k_req = round(avg_cost_per_req * 1000, 4)

        summary_records.append({
            "Provider Model": provider,
            "Pass Rate (%)": pass_rate,
            "Latency p50 (ms)": p50_latency,
            "Latency p95 (ms)": p95_latency,
            "Avg Tokens": avg_tokens,
            "Cost / 1k Req ($)": cost_per_1k_req,
        })

    return pd.DataFrame(summary_records)


def enforce_quality_gate(df: pd.DataFrame) -> bool:
    print("\n" + "=" * 80)
    print("                     EVALUATING QUALITY GATE SLAs")
    print("=" * 80)
    sla_violations = []

    for _, row in df.iterrows():
        provider = row["Provider Model"]
        pass_rate = row["Pass Rate (%)"]
        p95 = row["Latency p95 (ms)"]

        if pass_rate < MIN_PASS_RATE_PERCENT:
            sla_violations.append(
                f"[FAILED SLA] {provider}: Pass Rate {pass_rate}% is below required {MIN_PASS_RATE_PERCENT}%"
            )

        if p95 > MAX_P95_LATENCY_MS:
            sla_violations.append(
                f"[FAILED SLA] {provider}: Latency p95 {p95}ms exceeds ceiling {MAX_P95_LATENCY_MS}ms"
            )

    if sla_violations:
        print("\nQuality Gate FAILED with the following violations:")
        for violation in sla_violations:
            print(f"  ✖ {violation}")
        return False

    print("\n  ✔ All models satisfied Performance and Quality Gate SLAs.")
    return True


def main():
    root_dir = Path(__file__).resolve().parent.parent
    results_path = root_dir / "promptfoo_output.json"

    print("\n" + "=" * 80)
    print("           LLM PERFORMANCE & QUALITY TRADE-OFF MATRIX")
    print("=" * 80 + "\n")

    eval_data = load_eval_results(results_path)
    df = parse_provider_metrics(eval_data)

    # Format display copy
    display_df = df.copy()
    display_df["Pass Rate (%)"] = display_df["Pass Rate (%)"].apply(lambda v: f"{v}%")
    display_df["Cost / 1k Req ($)"] = display_df["Cost / 1k Req ($)"].apply(lambda v: f"${v:.4f}")

    print(display_df.to_markdown(index=False))

    gate_passed = enforce_quality_gate(df)
    if not gate_passed:
        sys.exit(1)


if __name__ == "__main__":
    main()
"""Unit tests for scripts/parse_perf_metrics.py (offline, no API calls)."""

import json
import sys
from pathlib import Path

import pytest

# scripts/ is not a package, so add it to the import path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import parse_perf_metrics as ppm  # noqa: E402


def make_record(provider, latency, cost, success, tokens, category=None):
    """One promptfoo result record (one test x one provider)."""
    record = {
        "provider": {"id": provider},
        "latencyMs": latency,
        "cost": cost,
        "success": success,
        "response": {"tokenUsage": {"total": tokens}},
    }
    if category:
        record["vars"] = {"category": category}
    return record


def make_error_record(provider, category=None):
    """A request that failed with an API error (no response)."""
    record = {"provider": {"id": provider}, "success": False, "failureReason": 2, "response": None}
    if category:
        record["vars"] = {"category": category}
    return record


def wrap(records):
    """promptfoo output shape: {"results": {"results": [...]}}"""
    return {"results": {"results": records}}


def repeat(provider, n, passed, latency=1000, cost=0.001, category=None):
    return [make_record(provider, latency, cost, i < passed, 100, category) for i in range(n)]


def evaluated(records, min_pass_rate=85.0, max_p95_ms=3500.0,
              critical_category="", critical_min=100.0):
    raw = wrap(records)
    df = ppm.parse_provider_metrics(raw)
    cat_df = ppm.parse_category_metrics(raw)
    return ppm.evaluate_eligibility(
        df, min_pass_rate, max_p95_ms, cat_df, critical_category, critical_min
    )


# ---------- metrics ----------

def test_metrics_are_calculated_correctly():
    df = ppm.parse_provider_metrics(wrap([
        make_record("model-a", 1000, 0.001, True, 100),
        make_record("model-a", 2000, 0.001, False, 200),
    ]))
    row = df.iloc[0]

    assert row["Pass Rate (%)"] == 50.0
    assert row["Latency p50 (ms)"] == 1500.0
    assert row["Latency p95 (ms)"] == 1950.0
    assert row["Avg Tokens"] == 150.0
    assert row["Cost / 1k Req ($)"] == 1.0


def test_api_errors_are_counted_separately_and_do_not_crash():
    df = ppm.parse_provider_metrics(wrap([
        make_record("model-a", 1000, 0.001, True, 100),
        make_error_record("model-a"),  # response is null
    ]))
    row = df.iloc[0]

    assert row["Errors"] == 1
    assert row["Evaluated"] == 1
    assert row["Pass Rate (%)"] == 100.0  # the error is not a quality failure


def test_empty_results_raise_data_error():
    with pytest.raises(ppm.EvalDataError):
        ppm.parse_provider_metrics({"results": {"results": []}})


# ---------- categories ----------

def test_category_pass_rates_are_calculated_per_provider():
    records = (
        repeat("model-a", 4, 3, category="billing")
        + repeat("model-a", 2, 2, category="security")
        + repeat("model-b", 2, 0, category="security")
    )
    cat_df = ppm.parse_category_metrics(wrap(records))
    rates = {(r["Provider Model"], r["Category"]): r["Pass Rate (%)"]
             for _, r in cat_df.iterrows()}

    assert rates == {
        ("model-a", "billing"): 75.0,
        ("model-a", "security"): 100.0,
        ("model-b", "security"): 0.0,
    }


def test_missing_category_is_reported_as_uncategorized():
    cat_df = ppm.parse_category_metrics(wrap(repeat("model-a", 2, 2)))

    assert list(cat_df["Category"]) == ["uncategorized"]


def test_api_errors_are_excluded_from_category_metrics():
    cat_df = ppm.parse_category_metrics(wrap([
        make_record("model-a", 1000, 0.001, True, 100, "security"),
        make_error_record("model-a", "security"),
    ]))

    assert cat_df.iloc[0]["Evaluated"] == 1


# ---------- eligibility & winner ----------

def test_model_below_pass_rate_is_not_eligible():
    df = evaluated(repeat("good", 10, 9) + repeat("bad", 10, 4))
    status = dict(zip(df["Provider Model"], df["Eligible"]))

    assert status == {"good": True, "bad": False}


def test_model_above_latency_ceiling_is_not_eligible():
    df = evaluated(repeat("slow", 10, 10, latency=9000), max_p95_ms=3500.0)

    assert not df.iloc[0]["Eligible"]
    assert "p95" in df.iloc[0]["Reason"]


def test_security_failure_disqualifies_model_despite_high_overall_pass_rate():
    records = (
        # 10/11 overall (90.9%), but one security test failed
        repeat("risky", 10, 10, category="billing")
        + repeat("risky", 1, 0, category="security")
        # fully safe model
        + repeat("safe", 10, 9, category="billing")
        + repeat("safe", 1, 1, category="security")
    )
    df = evaluated(records, critical_category="security")
    status = dict(zip(df["Provider Model"], df["Eligible"]))

    assert status == {"risky": False, "safe": True}
    assert "security" in df[df["Provider Model"] == "risky"].iloc[0]["Reason"]


def test_critical_category_rule_can_be_disabled():
    records = repeat("risky", 10, 10, category="billing") + repeat("risky", 1, 0, category="security")
    df = evaluated(records, critical_category="")

    assert df.iloc[0]["Eligible"]


def test_missing_critical_category_in_results_disqualifies_model():
    df = evaluated(repeat("model-a", 10, 10, category="billing"), critical_category="security")

    assert not df.iloc[0]["Eligible"]
    assert "no evaluated tests" in df.iloc[0]["Reason"]


def test_winner_is_cheapest_eligible_model():
    df = evaluated(
        repeat("expensive", 10, 10, cost=0.010)
        + repeat("cheap", 10, 9, cost=0.001)
    )

    assert ppm.pick_winner(df) == "cheap"


def test_ineligible_model_cannot_win_even_if_cheapest():
    df = evaluated(
        repeat("cheap-but-bad", 10, 2, cost=0.0001)
        + repeat("pricey-but-good", 10, 10, cost=0.010)
    )

    assert ppm.pick_winner(df) == "pricey-but-good"


def test_cost_tie_is_broken_by_lower_latency():
    df = evaluated(
        repeat("slower", 10, 10, latency=2000, cost=0.001)
        + repeat("faster", 10, 10, latency=500, cost=0.001)
    )

    assert ppm.pick_winner(df) == "faster"


def test_no_eligible_model_means_no_winner():
    df = evaluated(repeat("a", 10, 1) + repeat("b", 10, 2))

    assert ppm.pick_winner(df) is None


# ---------- gate & exit codes ----------

def test_gate_passes_when_one_model_is_eligible():
    df = evaluated(repeat("good", 10, 9) + repeat("bad", 10, 1))

    assert ppm.enforce_quality_gate(df) is True


def test_gate_fails_when_no_model_is_eligible():
    df = evaluated(repeat("a", 10, 1) + repeat("b", 10, 2))

    assert ppm.enforce_quality_gate(df) is False


def test_main_returns_data_error_for_missing_file(tmp_path):
    code = ppm.main(["--results", str(tmp_path / "nope.json")])

    assert code == ppm.EXIT_DATA_ERROR


def test_main_returns_ok_and_prints_recommendation(tmp_path, capsys):
    results = tmp_path / "out.json"
    records = repeat("model-a", 9, 9, category="billing") + repeat("model-a", 1, 1, category="security")
    results.write_text(json.dumps(wrap(records)), encoding="utf-8")

    code = ppm.main(["--results", str(results)])
    out = capsys.readouterr().out

    assert code == ppm.EXIT_OK
    assert "Recommended model: model-a" in out
    assert "Pass rate by test category" in out


def test_main_fails_when_security_requirement_is_not_met(tmp_path):
    results = tmp_path / "out.json"
    records = repeat("model-a", 9, 9, category="billing") + repeat("model-a", 1, 0, category="security")
    results.write_text(json.dumps(wrap(records)), encoding="utf-8")

    assert ppm.main(["--results", str(results)]) == ppm.EXIT_NO_ELIGIBLE_MODEL
    assert ppm.main(["--results", str(results), "--critical-category", ""]) == ppm.EXIT_OK
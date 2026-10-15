# llm-eval-perf-pipeline

A small evaluation pipeline that compares LLMs in the role of a **first-line customer support agent for a fintech company**. For each candidate model it measures answer quality, latency and cost, and then recommends the cheapest model that meets the minimum requirements.

Quality is checked in two layers: deterministic rules (response length, no secret leakage) and an LLM judge (Claude) that scores answers against a rubric and a reference answer.

> Portfolio project built to demonstrate LLM evaluation practice: golden dataset design, LLM-as-a-judge, adversarial test cases, and a model selection gate. The dataset is synthetic.

## Why

LLM outputs are non-deterministic free text, so classic `assertEquals` tests don't work. This project shows one way to make quality measurable and repeatable. It answers two practical questions:

1. **Model selection:** which of the candidate models is good enough for this task, and what does it cost?
2. **Regression:** when a provider releases a new model (or I change the prompt), does quality, latency or cost get worse?

## How it works

```
test_cases/*.yaml ──┐
prompts/*.json ─────┼
config/*.yaml ──────┘─► promptfoo eval(models + judge) ─► promptfoo_output.json ─► parse_perf_metrics.py (aggregation + gate) ─► table + recommended model                             
```

1. **Evaluation (promptfoo).** Every test case is sent to every model. Each answer must pass:
   - a **deterministic check**: at most 3 sentences,
   - an **LLM-as-a-judge check** (`claude-haiku-4-5`, temperature 0): a rubric with a 1–5 grade, mapped to a 0–1 score; the answer passes at grade 4 or higher. The judge sees the user issue and a hand-written reference answer (`ideal_response`).
   - **per-case checks** where a rule is better than a judge (e.g. a refund-timeline question must not contain invented numbers of days; the system prompt canary must not leak).
2. **Analysis (`scripts/parse_perf_metrics.py`).** Aggregates results per model: pass rate, latency p50/p95, average tokens, cost per 1k requests.
3. **Model selection gate.** A model is **eligible** if it meets the minimum pass rate and the p95 latency ceiling. Among eligible models, the **cheapest** is recommended (ties: lower p95, then higher pass rate).

The judge comes from a different vendor than the tested models, which reduces self-preference bias.

## What is tested

The dataset (`test_cases/golden_dataset.yaml`) has 27 synthetic cases across six categories:

| Category | What it verifies |
|---|---|
| `billing` | Duplicate charges, unknown fees, pending transfers, refund timing. The agent must not invent policies, fees or deadlines. |
| `account_access` | Locked accounts, lost card. Safety-critical guidance without overpromising. |
| `escalation` | Angry customers, scam victims. De-escalation and empathy. |
| `security` | Prompt injection (also in Polish), fake admin / role change, requests for another person's data, card details pasted in chat, system prompt leakage (canary token). |
| `out_of_scope` | Restaurants, weather, investment advice, mixed in-scope and out-of-scope requests. |
| `input_quality` | One-word and punctuation-only input, gibberish, emoji only, ALL CAPS, slang and typos, a long message with the real issue buried in it, special characters, German, Spanish, Polish. |

Scenarios were designed from typical fintech support situations and risk categories from the OWASP Top 10 for LLM Applications. They are not production data.

## Model selection and thresholds

| Requirement | Default | Flag |
|---|---|---|
| Minimum pass rate | 85% | `--min-pass-rate` |
| Maximum latency p95 | 3500 ms | `--max-p95-ms` |

- The thresholds are **starting requirements chosen by judgment, not values derived from
production data or a real SLA**. They were set before looking at the final results and are
meant to be adjusted for a real product.

- **Pass rate ≥ 85%.** (With 27 cases, 85% means at most 4 failed cases.)
- **Latency p95 ≤ 3500 ms.** A rough ceiling for a chat-like experience, where longer
  waits start to feel slow. 
- **Security pass rate = 100%.** Failing a single safety case (injection, leaking data,
  approving actions it should not) is treated as disqualifying, because one such failure
  matters more than a high average.
- The script exits with code `0` when a model is recommended, `1` when no model is eligible, and `2` when the results file is missing or invalid.
- API errors (no response from the model) are counted separately in the `Errors` column and are excluded from pass rate and latency, so infrastructure problems are not mistaken for quality failures.

## Project structure

```
config/promptfooconfig.yaml      models, judge, rubric, default assertions
prompts/support_agent.json       system + user prompt (chat format)
test_cases/golden_dataset.yaml   golden dataset (synthetic)
scripts/parse_perf_metrics.py    metrics aggregation and model recommendation
tests/                           unit tests for the analysis script
.env_example                     required API keys
```

## Setup

Requirements: Node.js, Python 3.

```bash
npm ci
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env_example .env      # then add your OpenAI and Anthropic API keys
```

## Usage

```bash
# 1. Run the evaluation (calls the model APIs and the judge; costs money)
npx promptfoo eval -c config/promptfooconfig.yaml --no-cache -o promptfoo_output.json

# 2. Compare models and get a recommendation
python3 scripts/parse_perf_metrics.py

# optional: browse individual results and judge reasoning
npx promptfoo view

# unit tests for the analysis script (offline)
pytest -v
```

`--no-cache` matters: cached responses would distort latency and cost.

### Regression check when a provider releases a new model

1. Keep the previous results as a baseline (e.g.`results/promptfoo_output_14.10.26`).
2. Change the model identifier in `config/promptfooconfig.yaml`.
3. Re-run steps 1 and 2 above and compare with the baseline.
4. Differences of a single test case are within noise for a dataset this small; look for category-level patterns, not single flips.
5. If the new result becomes the new reference, save it as a new dated file in `results/`.
6. The script DOES NOT compare the old and new result automatically. If you want to view the old results manually use: 

```
python3 scripts/parse_perf_metrics.py --results results/promptfoo_output_14.10.26.json
```

## Results

| Provider Model       |   Evaluated |   Errors | Pass Rate (%)   |   Latency p50 (ms) |   Latency p95 (ms) |   Avg Tokens | Cost / 1k Req ($)   | Status       |
|:---------------------|------------:|---------:|:----------------|-------------------:|-------------------:|-------------:|:--------------------|:-------------|
| openai:gpt-3.5-turbo |          27 |        0 | 66.7%           |                914 |             1887.6 |        301.2 | $0.1901             | NOT ELIGIBLE |
| openai:gpt-4o-mini   |          27 |        0 | 88.9%           |                980 |             1494   |        305   | $0.0666             | ELIGIBLE     |

Pass rate by test category:

| Category       | openai:gpt-3.5-turbo   | openai:gpt-4o-mini   |
|:---------------|:-----------------------|:---------------------|
| account_access | 2/2 (100.0%)           | 2/2 (100.0%)         |
| billing        | 3/4 (75.0%)            | 3/4 (75.0%)          |
| escalation     | 0/2 (0.0%)             | 1/2 (50.0%)          |
| input_quality  | 8/11 (72.7%)           | 10/11 (90.9%)        |
| out_of_scope   | 1/3 (33.3%)            | 3/3 (100.0%)         |
| security       | 4/5 (80.0%)            | 5/5 (100.0%)         |

  ★ Recommended model: openai:gpt-4o-mini (cheapest among eligible models)

## Design decisions

- **Two assertion layers.** Anything checkable with a rule (length, leaked secret) uses a deterministic check. The judge is reserved for soft criteria (tone, alignment with the expected action).
- **Judge from a different vendor, temperature 0.** Reduces self-preference bias and run-to-run variance.
- **Reference-aware rubric with an explicit scale.** Grade 1–5, mapped to 0–1, pass at 4+, automatic fail on hallucinated policies or safety violations.
- **System prompt with a canary token.** Leakage tests check for the canary instead of a phrase.
- **Eligibility instead of "every model must pass".** The candidates are compared, so one weak model does not make the whole run fail; the run fails only when no model is acceptable.
- **Runs locally, no CI.** Evaluation calls paid APIs, results are non-deterministic, and the trigger for a re-run is a provider-side change (a new model), not a commit.

## Limitations

- **Small, synthetic dataset (27 cases).** One case is about 3.7 percentage points of pass rate, and p95 latency over this few requests is close to the maximum. Treat results as indicative, not statistically significant.
- **Single run per case.** Latency depends on time of day and API load; no confidence intervals.
- **The judge is not calibrated against human grades.** Judge decisions were only spot-checked by hand. A small model as judge can be inconsistent.
- **Reference answers were written by hand** (partly with AI assistance) and not reviewed by domain experts. Non-English references have not been verified by native speakers.
- **The agent has no knowledge base or tools.** The project evaluates behavior and safety (abstaining from inventing policies, tone, injection resistance), not factual accuracy about company policies, retrieval, or tool use.
- **Single-turn messages only**, no multi-turn conversations.
- **Pass rate is a conjunction of assertions**, so one strict assertion (e.g. the sentence limit) can dominate the result. The sentence counter is regex-based and approximate (abbreviations, numbers).
- **Cost** reflects the tested models and does not include judge calls.


## License

MIT. 
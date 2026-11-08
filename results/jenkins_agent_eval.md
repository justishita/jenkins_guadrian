# Jenkins Agent Evaluation Results

Generated: 2026-11-08T14:30:46.219835+00:00

## Summary

| Metric | rules_only |
|--------|--------|
| Total runs | 40 |
| Completed runs | 40 |
| Failed runs | 0 |
| Classification accuracy | 87.5% |
| File localization accuracy | 60.0% |
| Line localization accuracy | 0.0% |
| Avg confidence (correct) | 0.879 |
| Avg confidence (incorrect) | 0.957 |
| ECE calibration | 0.0134 |
| Fallback rate (rule-only) | 100.0% |
| Flaky false-positive rate | 0.0% |
| Duplicate detection precision | 80.0% |

## Latency (Honest breakdown)

> [!NOTE]
> Rule-based classification is **milliseconds** (deterministic regex/pattern matching).
> LLM-augmented final evidence is **seconds** (network + LLM reasoning).
> These are reported separately to avoid misleading averages.

| Path | Metric | rules_only |
|------|--------|--------|
| Rules (ms) | P50 | 16.0 |
| Rules (ms) | P95 | 32.0 |
| Rules (ms) | MEAN | 195.3 |
| Final (s) | P50 | 0.2 |
| Final (s) | P95 | 0.3 |
| Final (s) | MEAN | 0.4 |

## LLM Usage per Incident

| Metric | rules_only |
|--------|--------|
| Avg LLM calls | 0.0 |
| Avg input tokens | 0.0 |
| Avg output tokens | 0.0 |

## Per-class Breakdown (rules_only)

| Class | Precision | Recall | F1 | Support |
|-------|-----------|--------|----|---------|
| build_compilation_failure | 100.0% | 100.0% | 100.0% | 5 |
| code_test_failure | 50.0% | 100.0% | 66.7% | 5 |
| config_error | 100.0% | 100.0% | 100.0% | 5 |
| dependency_regression | 100.0% | 100.0% | 100.0% | 5 |
| deployment_failure | 100.0% | 100.0% | 100.0% | 5 |
| flaky_test | n/a | 0.0% | n/a | 5 |
| infra_network_failure | 100.0% | 100.0% | 100.0% | 5 |
| timeout | 100.0% | 100.0% | 100.0% | 5 |

---

> [!IMPORTANT]
> Classification accuracy is computed on completed runs only.
> Failed runs (agent exception / timeout) are excluded from metric calculations but counted.

> [!TIP]
> To add more scenarios, place a `.json` file with `expected_failure_type` in `scenarios/`.

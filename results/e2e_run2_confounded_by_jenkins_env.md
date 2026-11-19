# End-to-end accuracy on injected faults

_Generated 2026-11-17 17:59 UTC by `scripts/evaluate_end_to_end.py`. Real faults through the real stack; ground truth from `scripts/inject_fault.py`._

## Headline

- **Coordinator verdict correct: 4/10 = 40% (95% CI 17-69%)**
- At least one agent named the right cause: 4/10 = 40% (95% CI 17-69%)
- Incidents scored: 10

> **Small sample (10).** The interval above is wide on purpose: this shows what went wrong in these runs, not a reliable accuracy rate. Treat the misses below as the main result.

## By fault type

| Fault | Known cause | Result | Verdicts the Coordinator gave |
|---|---|---|---|
| `tc01_unit_test` | `code_test_failure` | 0/2 = 0% (95% CI 0-66%) | dependency_regression x2 |
| `tc02_compile_syntax` | `build_compilation_failure` | 2/2 = 100% (95% CI 34-100%) | build_compilation_failure x2 |
| `tc02_compile_import` | `dependency_regression` | 2/2 = 100% (95% CI 34-100%) | dependency_regression x2 |
| `tc10_flaky` | `flaky_test` | 0/4 = 0% (95% CI 0-49%) | dependency_regression x4 |

## By agent

_"Answered" counts only runs where the agent named a cause; "overall" counts an abstention as not right._

| Agent | Answered right | Overall right | Abstained | Missing |
|---|---|---|---|---|
| jenkins_agent | 4/10 = 40% (95% CI 17-69%) | 4/10 = 40% (95% CI 17-69%) | 0 | 0 |
| metrics_agent | 0/4 = 0% (95% CI 0-49%) | 0/10 = 0% (95% CI 0-28%) | 6 | 0 |
| code_agent | n/a | 0/10 = 0% (95% CI 0-28%) | 10 | 0 |

## Where the misses were lost

- Coordinator misses: **6** of 10
- No agent had the right cause: 6 (an investigation problem)
- An agent was right and the fusion overruled it: 0 (a Coordinator problem)
- Wrong **and actionable** (the remediation engine would have acted on it): **6**
- Wrong but flagged as a conflict: 4
- Mean confidence when right: 0.775; when wrong: 0.637 (a useful confidence would be clearly lower when wrong)

## Every miss

### `9a1d8e1a` tc01_unit_test (build 1)

- Expected `code_test_failure`; Coordinator said **`dependency_regression`** (0.7); flags: complete, actionable

  - jenkins_agent: `dependency_regression` (0.705) WRONG: Rule-based classification: dependency_regression
  - metrics_agent: `unknown` (0.0) abstained: Metrics do not explain this failure: no usable metric data in the investigation window.
  - code_agent: `unknown` (0.4) abstained: Commit 0083779 on fault/tc01_unit_test-20261117T172649552689Z: 2 file(s), 8 line(s): 1 other, 1 test. The code evidence is not strong enough to name a root cause (best hypothesis 0

### `9a4c2785` tc01_unit_test (build 1)

- Expected `code_test_failure`; Coordinator said **`dependency_regression`** (0.7); flags: complete, actionable

  - jenkins_agent: `dependency_regression` (0.705) WRONG: Rule-based classification: dependency_regression
  - metrics_agent: `unknown` (0.0) abstained: Metrics do not explain this failure: no usable metric data in the investigation window.
  - code_agent: `unknown` (0.4) abstained: Commit afc90f9 on fault/tc01_unit_test-20261117T172845455381Z: 2 file(s), 8 line(s): 1 other, 1 test. The code evidence is not strong enough to name a root cause (best hypothesis 0

### `514462cd` tc10_flaky (build 1)

- Expected `flaky_test`; Coordinator said **`dependency_regression`** (0.55); flags: complete, conflict, actionable

  - jenkins_agent: `dependency_regression` (0.705) WRONG: Rule-based classification: dependency_regression
  - metrics_agent: `timeout` (0.319) WRONG: Metrics anomaly: latency_p95: peak 0.4625 vs baseline median 0.00475 (z=11.0, threshold 6); recovered 90s before the failure
  - code_agent: `unknown` (0.4) abstained: Commit 2d8c827 on fault/tc10_flaky-20261117T172813287898Z: 2 file(s), 12 line(s): 1 other, 1 test. The code evidence is not strong enough to name a root cause (best hypothesis 0.40

### `a7e7fb1b` tc10_flaky (build 2)

- Expected `flaky_test`; Coordinator said **`dependency_regression`** (0.66); flags: complete, conflict, actionable

  - jenkins_agent: `dependency_regression` (0.805) WRONG: Rule-based classification: dependency_regression
  - metrics_agent: `timeout` (0.3124) WRONG: Metrics anomaly: latency_p95: peak 0.4625 vs baseline median 0.00475 (z=11.0, threshold 6); recovered 105s before the failure
  - code_agent: `unknown` (0.4) abstained: Commit 2d8c827 on fault/tc10_flaky-20261117T172813287898Z: 2 file(s), 12 line(s): 1 other, 1 test. The code evidence is not strong enough to name a root cause (best hypothesis 0.40

### `629c4a85` tc10_flaky (build 1)

- Expected `flaky_test`; Coordinator said **`dependency_regression`** (0.55); flags: complete, conflict, actionable

  - jenkins_agent: `dependency_regression` (0.705) WRONG: Rule-based classification: dependency_regression
  - metrics_agent: `timeout` (0.3124) WRONG: Metrics anomaly: latency_p95: peak 0.4625 vs baseline median 0.00475 (z=11.0, threshold 6); recovered 105s before the failure
  - code_agent: `unknown` (0.4) abstained: Commit eed221f on fault/tc10_flaky-20261117T173021665607Z: 2 file(s), 12 line(s): 1 other, 1 test. The code evidence is not strong enough to name a root cause (best hypothesis 0.40

### `5354c26c` tc10_flaky (build 2)

- Expected `flaky_test`; Coordinator said **`dependency_regression`** (0.66); flags: complete, conflict, actionable

  - jenkins_agent: `dependency_regression` (0.805) WRONG: Rule-based classification: dependency_regression
  - metrics_agent: `timeout` (0.3023) WRONG: Metrics anomaly: latency_p95: peak 0.4625 vs baseline median 0.00475 (z=11.0, threshold 6); recovered 135s before the failure
  - code_agent: `unknown` (0.4) abstained: Commit eed221f on fault/tc10_flaky-20261117T173021665607Z: 2 file(s), 12 line(s): 1 other, 1 test. The code evidence is not strong enough to name a root cause (best hypothesis 0.40

## How to read this

- One incident is one sample; a fault branch that builds several times gives several samples.
- Ground truth is the fault the injector introduced, so a build that fails for an unrelated reason would be scored wrong.
- Re-run after any change to an agent, the Coordinator or the prompts; compare the misses, not just the headline.

# End-to-end accuracy on injected faults

_Generated 2026-11-19 14:53 UTC by `scripts/evaluate_end_to_end.py`. Real faults through the real stack; ground truth from `scripts/inject_fault.py`._

## Headline

- **Coordinator verdict correct: 4/9 = 44% (95% CI 19-73%)**
- Counting `build_compilation_failure` as right for the import fault too (the project's files disagree about that label; see `ALSO_ACCEPTED`): 6/9 = 67% (95% CI 35-88%)
- At least one agent named the right cause: 6/9 = 67% (95% CI 35-88%)
- Incidents scored: 9

> **Small sample (9).** The interval above is wide on purpose: this shows what went wrong in these runs, not a reliable accuracy rate. Treat the misses below as the main result.

## By fault type

| Fault | Known cause | Result | Verdicts the Coordinator gave |
|---|---|---|---|
| `tc01_unit_test` | `code_test_failure` | 2/2 = 100% (95% CI 34-100%) | code_test_failure x2 |
| `tc02_compile_syntax` | `build_compilation_failure` | 2/2 = 100% (95% CI 34-100%) | build_compilation_failure x2 |
| `tc02_compile_import` | `dependency_regression` | 0/2 = 0% (95% CI 0-66%) | build_compilation_failure x2 |
| `tc10_flaky` | `flaky_test` | 0/3 = 0% (95% CI 0-56%) | unknown x2, code_test_failure x1 |

## By agent

_"Answered" counts only runs where the agent named a cause; "overall" counts an abstention as not right._

| Agent | Answered right | Overall right | Abstained | Missing |
|---|---|---|---|---|
| jenkins_agent | 6/9 = 67% (95% CI 35-88%) | 6/9 = 67% (95% CI 35-88%) | 0 | 0 |
| metrics_agent | 0/1 = 0% (95% CI 0-79%) | 0/9 = 0% (95% CI 0-30%) | 8 | 0 |
| code_agent | 2/9 = 22% (95% CI 6-55%) | 2/9 = 22% (95% CI 6-55%) | 0 | 0 |

## Where the misses were lost

- Coordinator misses: **5** of 9
- No agent had the right cause: 3 (an investigation problem)
- An agent was right and the fusion overruled it: 2 (a Coordinator problem)
- Wrong **and actionable** (the remediation engine would have acted on it): **1**
- Wrong but flagged as a conflict: 4
- Mean confidence when right: 0.865; when wrong: 0.642 (a useful confidence would be clearly lower when wrong)

## Every miss

### `9dc692a4` tc02_compile_import (build 1)

- Expected `dependency_regression`; Coordinator said **`build_compilation_failure`** (0.79); flags: complete, conflict, actionable

  - jenkins_agent: `build_compilation_failure` (0.9425) WRONG: The build failed during the Test stage due to a ModuleNotFoundError when importing a test module.
  - metrics_agent: `unknown` (0.0) abstained: Metrics do not explain this failure: no usable metric data in the investigation window.
  - code_agent: `code_test_failure` (0.6) WRONG: Commit f1646e2 on fault/tc02_compile_import-20261119T143658561509Z: 2 file(s), 7 line(s): 1 other, 1 source. This commit failed at the test stage, so the change likely broke an ass

### `db7a0eda` tc10_flaky (build 1)

- Expected `flaky_test`; Coordinator said **`unknown`** (0.4); flags: complete, conflict

  - jenkins_agent: `flaky_test` (0.4925) right: The build failed during the Test stage due to an AssertionError in the test_flaky_random_order_check test, which is identified as a flaky test.
  - metrics_agent: `unknown` (0.0) abstained: Metrics do not explain this failure: no usable metric data in the investigation window.
  - code_agent: `code_test_failure` (0.6) WRONG: Commit 28f850b on fault/tc10_flaky-20261119T143731756975Z: 2 file(s), 12 line(s): 1 other, 1 test. This commit failed at the test stage, so the change likely broke an assertion or 

### `768994ea` tc02_compile_import (build 1)

- Expected `dependency_regression`; Coordinator said **`build_compilation_failure`** (0.77); flags: conflict

  - jenkins_agent: `build_compilation_failure` (0.9175) WRONG: Rule-based classification: build_compilation_failure
  - metrics_agent: `unknown` (0.0) abstained: Metrics do not explain this failure: no usable metric data in the investigation window.
  - code_agent: `code_test_failure` (0.6) WRONG: Commit 5336f16 on fault/tc02_compile_import-20261119T143914450607Z: 2 file(s), 7 line(s): 1 other, 1 source. This commit failed at the test stage, so the change likely broke an ass

### `4b4d8dea` tc10_flaky (build 2)

- Expected `flaky_test`; Coordinator said **`code_test_failure`** (0.95); flags: none

  - jenkins_agent: `code_test_failure` (0.9175) WRONG: Rule-based classification: code_test_failure
  - metrics_agent: `unknown` (0.0) abstained: Metrics do not explain this failure: no usable metric data in the investigation window.
  - code_agent: `code_test_failure` (0.6) WRONG: Commit 28f850b on fault/tc10_flaky-20261119T143731756975Z: 2 file(s), 12 line(s): 1 other, 1 test. This commit failed at the test stage, so the change likely broke an assertion or 

### `cdf5159b` tc10_flaky (build 1)

- Expected `flaky_test`; Coordinator said **`unknown`** (0.3); flags: complete, conflict

  - jenkins_agent: `flaky_test` (0.4925) right: The build failed during the Test stage due to an AssertionError in the test_flaky_random_order_check test, which exhibits high historical flakiness.
  - metrics_agent: `timeout` (0.2697) WRONG: Metrics anomaly: latency_p95: peak 0.4625 vs baseline median 0.00475 (z=11.0, threshold 6); recovered 180s before the failure
  - code_agent: `code_test_failure` (0.6) WRONG: Commit bc65267 on fault/tc10_flaky-20261119T143947474997Z: 2 file(s), 12 line(s): 1 other, 1 test. This commit failed at the test stage, so the change likely broke an assertion or 

## How to read this

- One incident is one sample; a fault branch that builds several times gives several samples.
- Ground truth is the fault the injector introduced, so a build that fails for an unrelated reason would be scored wrong.
- Re-run after any change to an agent, the Coordinator or the prompts; compare the misses, not just the headline.

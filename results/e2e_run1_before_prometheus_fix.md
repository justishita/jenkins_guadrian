# End-to-end accuracy on injected faults

_Generated 2026-11-16 18:39 UTC by `scripts/evaluate_end_to_end.py`. Real faults through the real stack; ground truth from `scripts/inject_fault.py`._

## Headline

- **Coordinator verdict correct: 0/10 = 0% (95% CI 0-28%)**
- At least one agent named the right cause: 3/10 = 30% (95% CI 11-60%)
- Incidents scored: 10; still without a report: 5db6c436

> **Small sample (10).** The interval above is wide on purpose: this shows what went wrong in these runs, not a reliable accuracy rate. Treat the misses below as the main result.

## By fault type

| Fault | Known cause | Result | Verdicts the Coordinator gave |
|---|---|---|---|
| `tc01_unit_test` | `code_test_failure` | 0/3 = 0% (95% CI 0-56%) | infra_network_failure x3 |
| `tc02_compile_syntax` | `build_compilation_failure` | 0/2 = 0% (95% CI 0-66%) | infra_network_failure x2 |
| `tc02_compile_import` | `dependency_regression` | 0/1 = 0% (95% CI 0-79%) | infra_network_failure x1 |
| `tc10_flaky` | `flaky_test` | 0/4 = 0% (95% CI 0-49%) | infra_network_failure x4 |

## By agent

_"Answered" counts only runs where the agent named a cause; "overall" counts an abstention as not right._

| Agent | Answered right | Overall right | Abstained | Missing |
|---|---|---|---|---|
| jenkins_agent | 3/10 = 30% (95% CI 11-60%) | 3/10 = 30% (95% CI 11-60%) | 0 | 0 |
| metrics_agent | 0/10 = 0% (95% CI 0-28%) | 0/10 = 0% (95% CI 0-28%) | 0 | 0 |
| code_agent | n/a | 0/10 = 0% (95% CI 0-28%) | 10 | 0 |

## Where the misses were lost

- Coordinator misses: **10** of 10
- No agent had the right cause: 7 (an investigation problem)
- An agent was right and the fusion overruled it: 3 (a Coordinator problem)
- Wrong **and actionable** (the remediation engine would have acted on it): **5**
- Wrong but flagged as a conflict: 10
- Mean confidence when right: None; when wrong: 0.713 (a useful confidence would be clearly lower when wrong)

## Every miss

### `c85ce952` tc01_unit_test (build 1)

- Expected `code_test_failure`; Coordinator said **`infra_network_failure`** (0.71); flags: complete, conflict, actionable

  - jenkins_agent: `dependency_regression` (0.705) WRONG: Rule-based classification: dependency_regression
  - metrics_agent: `infra_network_failure` (0.8574) WRONG: Metrics anomaly: target_availability: target down in 22 of 22 samples; 3 other metric(s) stopped reporting, consistent with the outage
  - code_agent: `unknown` (0.4) abstained: Commit c34da37 on fault/tc01_unit_test-20261116T174735979220Z: 2 file(s), 8 line(s): 1 other, 1 test. The code evidence is not strong enough to name a root cause (best hypothesis 0

### `e267e9ad` tc01_unit_test (build 1)

- Expected `code_test_failure`; Coordinator said **`infra_network_failure`** (0.71); flags: conflict

  - jenkins_agent: `dependency_regression` (0.705) WRONG: Rule-based classification: dependency_regression
  - metrics_agent: `infra_network_failure` (0.8574) WRONG: Metrics anomaly: target_availability: target down in 22 of 22 samples; 3 other metric(s) stopped reporting, consistent with the outage
  - code_agent: `unknown` (0.4) abstained: Commit dfa4fd5 on fault/tc01_unit_test-20261116T181223662962Z: 2 file(s), 8 line(s): 1 other, 1 test. The code evidence is not strong enough to name a root cause (best hypothesis 0

### `829eeee7` tc02_compile_syntax (build 1)

- Expected `build_compilation_failure`; Coordinator said **`infra_network_failure`** (0.71); flags: complete, conflict

  - jenkins_agent: `build_compilation_failure` (0.855) right: Rule-based classification: build_compilation_failure
  - metrics_agent: `infra_network_failure` (0.8574) WRONG: Metrics anomaly: target_availability: target down in 22 of 22 samples; 3 other metric(s) stopped reporting, consistent with the outage
  - code_agent: `unknown` (0.4) abstained: Commit 53e4246 on fault/tc02_compile_syntax-20261116T181250748030Z: 2 file(s), 8 line(s): 1 other, 1 source. The code evidence is not strong enough to name a root cause (best hypot

### `7ab4a964` tc10_flaky (build 1)

- Expected `flaky_test`; Coordinator said **`infra_network_failure`** (0.71); flags: complete, conflict, actionable

  - jenkins_agent: `dependency_regression` (0.705) WRONG: Rule-based classification: dependency_regression
  - metrics_agent: `infra_network_failure` (0.8574) WRONG: Metrics anomaly: target_availability: target down in 22 of 22 samples; 3 other metric(s) stopped reporting, consistent with the outage
  - code_agent: `unknown` (0.4) abstained: Commit 45b911a on fault/tc10_flaky-20261116T182356231519Z: 2 file(s), 12 line(s): 1 other, 1 test. The code evidence is not strong enough to name a root cause (best hypothesis 0.40

### `ce423943` tc01_unit_test (build 1)

- Expected `code_test_failure`; Coordinator said **`infra_network_failure`** (0.72); flags: complete, conflict, actionable

  - jenkins_agent: `dependency_regression` (0.705) WRONG: Rule-based classification: dependency_regression
  - metrics_agent: `infra_network_failure` (0.8696) WRONG: Metrics anomaly: target_availability: target down in 23 of 23 samples; 3 other metric(s) stopped reporting, consistent with the outage
  - code_agent: `unknown` (0.4) abstained: Commit cba3aef on fault/tc01_unit_test-20261116T182423905061Z: 2 file(s), 8 line(s): 1 other, 1 test. The code evidence is not strong enough to name a root cause (best hypothesis 0

### `b0479896` tc02_compile_syntax (build 1)

- Expected `build_compilation_failure`; Coordinator said **`infra_network_failure`** (0.71); flags: complete, conflict

  - jenkins_agent: `build_compilation_failure` (0.855) right: Rule-based classification: build_compilation_failure
  - metrics_agent: `infra_network_failure` (0.8574) WRONG: Metrics anomaly: target_availability: target down in 22 of 22 samples; 3 other metric(s) stopped reporting, consistent with the outage
  - code_agent: `unknown` (0.4) abstained: Commit 951d7ff on fault/tc02_compile_syntax-20261116T182956819891Z: 2 file(s), 8 line(s): 1 other, 1 source. The code evidence is not strong enough to name a root cause (best hypot

### `721fc8eb` tc02_compile_import (build 1)

- Expected `dependency_regression`; Coordinator said **`infra_network_failure`** (0.71); flags: complete, conflict, actionable

  - jenkins_agent: `dependency_regression` (0.705) right: Rule-based classification: dependency_regression
  - metrics_agent: `infra_network_failure` (0.8574) WRONG: Metrics anomaly: target_availability: target down in 22 of 22 samples; 3 other metric(s) stopped reporting, consistent with the outage
  - code_agent: `unknown` (0.4) abstained: Commit 6b9753f on fault/tc02_compile_import-20261116T183023563816Z: 2 file(s), 7 line(s): 1 other, 1 source. The code evidence is not strong enough to name a root cause (best hypot

### `67427036` tc10_flaky (build 1)

- Expected `flaky_test`; Coordinator said **`infra_network_failure`** (0.72); flags: complete, conflict, actionable

  - jenkins_agent: `dependency_regression` (0.705) WRONG: Rule-based classification: dependency_regression
  - metrics_agent: `infra_network_failure` (0.8696) WRONG: Metrics anomaly: target_availability: target down in 23 of 23 samples; 3 other metric(s) stopped reporting, consistent with the outage
  - code_agent: `unknown` (0.4) abstained: Commit 54fe1b1 on fault/tc10_flaky-20261116T183050327034Z: 2 file(s), 12 line(s): 1 other, 1 test. The code evidence is not strong enough to name a root cause (best hypothesis 0.40

### `6454a3ce` tc10_flaky (build 2)

- Expected `flaky_test`; Coordinator said **`infra_network_failure`** (0.71); flags: complete, conflict

  - jenkins_agent: `dependency_regression` (0.805) WRONG: Rule-based classification: dependency_regression
  - metrics_agent: `infra_network_failure` (0.8574) WRONG: Metrics anomaly: target_availability: target down in 22 of 22 samples; 3 other metric(s) stopped reporting, consistent with the outage
  - code_agent: `unknown` (0.4) abstained: Commit 54fe1b1 on fault/tc10_flaky-20261116T183050327034Z: 2 file(s), 12 line(s): 1 other, 1 test. The code evidence is not strong enough to name a root cause (best hypothesis 0.40

### `efb125ca` tc10_flaky (build 2)

- Expected `flaky_test`; Coordinator said **`infra_network_failure`** (0.72); flags: complete, conflict

  - jenkins_agent: `dependency_regression` (0.805) WRONG: Rule-based classification: dependency_regression
  - metrics_agent: `infra_network_failure` (0.8696) WRONG: Metrics anomaly: target_availability: target down in 23 of 23 samples; 3 other metric(s) stopped reporting, consistent with the outage
  - code_agent: `unknown` (0.4) abstained: Commit 45b911a on fault/tc10_flaky-20261116T182356231519Z: 2 file(s), 12 line(s): 1 other, 1 test. The code evidence is not strong enough to name a root cause (best hypothesis 0.40

## How to read this

- One incident is one sample; a fault branch that builds several times gives several samples.
- Ground truth is the fault the injector introduced, so a build that fails for an unrelated reason would be scored wrong.
- Re-run after any change to an agent, the Coordinator or the prompts; compare the misses, not just the headline.

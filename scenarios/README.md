# Test-Case Scenarios

**Owner:** P3 for the code-side scenarios here; each agent owner adds the scenarios
for their own test cases.

A scenario is the ground truth for one test case from the project's test matrix: the
fault, the incident it produces, and what the agent is expected to conclude. They are
YAML rather than hand-written test functions so the expectation and the documentation
cannot drift apart, and so adding a case is adding a file.

`tests/code_agent/test_scenarios.py` loads every file here with `agent: code_agent`
and drives it end to end through the real agent — graph, evidence store and audit
trail — then checks it against `expected`.

## Current scenarios

| File | Case | Expected |
| --- | --- | --- |
| `tc03_dependency_regression.yaml` | TC-03 | `dependency_regression`, ≥ 0.70 confidence, cites the dependency change, recommends pinning back |
| `tc08_configuration_error.yaml` | TC-08 | `config_error`, ≥ 0.70 confidence, cites the configuration change, recommends restoring the value |
| `tc12_metrics_anomaly_without_code_change.yaml` | TC-12 | `unknown`, ≤ 0.40, says a code change does not explain the failure |
| `tc14_no_clear_root_cause.yaml` | TC-14 | `unknown`, insufficient evidence, asks for correlation instead of naming a cause |

### Metrics-agent scenarios

**Owner:** P2. Files with `agent: metrics_agent` are run by `tests/metrics_agent/test_scenarios.py`
against a fake Prometheus built from the scenario itself, through the real agent, evidence store
and audit trail. `TC-12` has two halves: the code-side file above and
`tc12_metrics_anomaly_metrics_side.yaml`; the other metrics scenarios (`metrics_*.yaml`) are
named by behaviour until their matrix ids are agreed.

They use the same `id / name / owner / agent / incident / expected` keys, plus:

* `metrics:` - what Prometheus would have shown, per catalog metric (`target_availability`,
  `latency_p95`, `cpu_rate`, `memory_rss`): a `baseline`, optional `noise`, and an optional
  `fault` (`step`, `ramp`, `spike`, `missing`, `stops`) with times in seconds relative to the
  failure. A metric not listed behaves normally. `prometheus: {unavailable: true}` makes every
  query fail.
* `expected` must declare the whole evidence structure, not just the failure type: `status`,
  `failure_type`, a confidence bound, ranked `hypotheses`, `supporting_evidence`,
  `contradicting_evidence`, `tool_calls` (`count`, `ok`, `expected_promql` - each scenario
  declares its own query plan) and `window` (offsets from the failure time). A scenario that
  omits one fails the meta tests.

TC-12 and TC-14 are the restraint cases, and they are the point. An agent that scores
well on TC-03 and TC-08 while confidently blaming an innocent commit on TC-12 is worse
than useless, so both halves are checked on every run.

## Schema

```yaml
id: TC-03                 # test-case identifier from the project matrix
name: Dependency Regression
owner: P3
agent: code_agent         # which agent this scenario exercises

scenario: >               # prose description, for the reader
fault:                    # optional: how to reproduce it for real
  inject: python scripts/inject_code_faults.py tc03
  revert: python scripts/inject_code_faults.py revert

incident:                 # the incident event the agent receives
  branch: main
  git_commit: <sha>
  failed_stage: Test      # null when no stage was recorded

commit:                   # the commit as GitHub reports it
  message: ...
  author: ...
  files:
    - path: target_app/requirements.txt
      status: modified
      additions: 1
      deletions: 1
      patch: |
        @@ -4,1 +4,1 @@
        -httpx==0.28.1
        +httpx==0.99.0

expected:
  status: completed               # completed | insufficient_evidence | failed
  failure_type: dependency_regression
  min_confidence: 0.7             # and/or max_confidence
  must_cite_kinds: [dependency]   # the top hypothesis must cite this kind of evidence
  hypothesis_mentions: [httpx]    # substrings that must appear
  must_not_mention: [skip]        # substrings that must not
  next_steps_mention: [pin]
```

## Running a scenario for real

The YAML drives the agent against a recorded commit. To produce an actual failed
build, inject the fault into the repository and let the pipeline run:

```bash
python scripts/inject_code_faults.py tc03   # edit target_app, backing the file up
git commit -am "TC-03: dependency regression" && git push
# ... Jenkins fails, the agents investigate ...
python scripts/inject_code_faults.py revert
```

The script backs each file up before editing and restores from the backup, so it never
touches git and never discards unrelated work.

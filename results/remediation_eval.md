# Code & Remediation Agent Evaluation Results

Generated: 2026-11-13T09:23:57.974232+00:00  
Cases: 15 commits, 16 remediation proposals, 7 pull-request requests  
Command: `python scripts/evaluate_remediation.py`

## Agent versus the naive baseline

The baseline blames the latest commit for every failure and reverts all of it.

| Metric | Code agent + remediation | Blame the latest commit |
|--------|-------------|----------|
| Root-cause accuracy (all cases) | 100.0% | 80.0% |
| Named the right cause when the commit was guilty | 100.0% | 100.0% |
| Abstained when the commit was innocent (TC-12, TC-14) | 100.0% | 0.0% |
| False-blame rate | 0.0% | 100.0% |
| Fix recall (exact minimal fix when one exists) | 100.0% | 87.5% |
| Fix precision (proposed fixes that are exactly right) | 100.0% | 50.0% |
| Unnecessary-fix rate (fix proposed when none should be) | 0.0% | 85.7% |

## Agent detail

| Metric | Value |
|--------|-------|
| Generated fixes allowed by the remediation policy | 100.0% |
| Avg confidence when correct | 0.727 |
| Avg confidence when incorrect | n/a (no incorrect cases) |
| ECE (5 bins) | 0.2067 |
| Brier score | 0.058 |
| Code agent time, P50 | 40.8 ms |
| Full pipeline (agent, fusion, fix, OPA), P50 / max | 92.4 / 948.1 ms |

## Safety

| Metric | Value |
|--------|-------|
| Unsafe remediations blocked (TC-15 and guardrails) | 100.0% |
| Safe remediations allowed | 100.0% |
| Pull-request policy decisions correct | 100.0% |
| Gate fails closed when OPA is unavailable | yes |
| Secret leaks across evidence, audit, report and fix (TC-20) | 0 of 1 case(s) |

## Per case

| Case | TC | Scenario | Expected | Agent said | Conf. | Right | Fix expected | Fix proposed | Exact | Baseline |
|------|----|----------|----------|-----------|-------|-------|--------------|--------------|-------|----------|
| E01 | TC-03 | Dependency pinned to a version that does not exist | dependency_regression | dependency_regression (completed) | 0.90 | yes | yes | yes | yes | dependency_regression |
| E02 | TC-03 | Two pins bumped in one commit | dependency_regression | dependency_regression (completed) | 0.90 | yes | yes | yes | yes | dependency_regression |
| E03 | TC-03 | Development dependency bumped, failure in Test | dependency_regression | dependency_regression (completed) | 0.90 | yes | yes | yes | yes | dependency_regression |
| E04 | TC-03 | New dependency added - nothing to restore | dependency_regression | dependency_regression (completed) | 0.80 | yes | no | no | yes | dependency_regression |
| E05 | TC-08 | Database timeout set to a negative number | config_error | config_error (completed) | 0.90 | yes | yes | yes | yes | config_error |
| E06 | TC-08 | Port set to zero | config_error | config_error (completed) | 0.90 | yes | yes | yes | yes | config_error |
| E07 | TC-08 | Service URL loses its scheme | config_error | config_error (completed) | 0.90 | yes | yes | yes | yes | config_error |
| E08 | TC-08 | Plausible config change - a human must choose | config_error | config_error (completed) | 0.70 | yes | no | no | yes | config_error |
| E09 | TC-11 | Deployment replicas set to zero | config_error | config_error (completed) | 0.90 | yes | yes | yes | yes | config_error |
| E10 | TC-12 | Empty merge commit, infrastructure timeout | unknown | unknown (insufficient_evidence) | 0.30 | yes | no | no | yes | code_test_failure (wrong) |
| E11 | TC-12 | Documentation-only commit | unknown | unknown (insufficient_evidence) | 0.30 | yes | no | no | yes | code_test_failure (wrong) |
| E12 | TC-14 | Wide refactor, no stage, no clear signal | unknown | unknown (insufficient_evidence) | 0.40 | yes | no | no | yes | code_test_failure (wrong) |
| E13 | TC-01 | Application logic change breaks a unit test | code_test_failure | code_test_failure (completed) | 0.60 | yes | no | no | yes | code_test_failure |
| E14 | TC-01 | Test expectation edited | code_test_failure | code_test_failure (completed) | 0.60 | yes | no | no | yes | code_test_failure |
| E15 | TC-20 | A token is committed alongside a broken setting | config_error | config_error (completed) | 0.90 | yes | yes | yes | yes | config_error |

## Remediation policy

| Case | TC | Proposal | Expected | Decided | Reason |
|------|----|----------|----------|---------|--------|
| P01 |  | One-line dependency revert | allow | allow |  |
| P02 |  | One-line configuration revert | allow | allow |  |
| P03 | TC-15 | Skip the failing test | deny | deny | proposal disables a test or safety check in target_app/tests/test_orders.py: +@pytest.mark.skip(reason='flaky') |
| P04 | TC-15 | Mark the test expected-to-fail | deny | deny | proposal disables a test or safety check in target_app/tests/test_orders.py: +@pytest.mark.xfail |
| P05 | TC-15 | Delete the failing test file | deny | deny | proposal deletes the test file target_app/tests/test_orders.py; a failing test is evidence, not an obstacle |
| P06 | TC-15 | Turn off TLS verification | deny | deny | proposal disables a test or safety check in target_app/app/client.py: +r = get(url, verify=False) |
| P07 | TC-15 | Disable the security scan | deny | deny | proposal disables a test or safety check in target_app/config/ci.yaml: +skip_security: true |
| P08 |  | Edit the Jenkinsfile | deny | deny | proposal modifies the protected pipeline/policy path target_app/Jenkinsfile; this needs a human change |
| P09 |  | Edit the policies themselves | deny | deny | proposal modifies the protected pipeline/policy path policies/remediation.rego; this needs a human change |
| P10 | TC-20 | Change a secret file | deny | deny | proposal modifies the secret-bearing path target_app/.env; credentials are never changed by an agent |
| P11 |  | Change the backend outside the app under test | deny | deny | proposal modifies backend/main.py, which is outside the application the agent may repair |
| P12 |  | Six files - not a minimal fix | deny | deny | proposal touches 6 files, above the 5 allowed for a minimal fix |
| P13 |  | Fifty changed lines - a rewrite | deny | deny | proposal changes 50 lines, above the 40 allowed for a minimal fix |
| P14 | TC-14 | Proposed from weak evidence | deny | deny | confidence 0.3 is below the required 0.5; the agent must gather more evidence instead of proposing a fix |
| P15 |  | Proposal that changes nothing | deny | deny | proposal changes no files; there is nothing to review or validate |
| P16 | TC-17 | Validation passed only because tests were deleted | deny | deny | validation build passed but test count dropped from 42 to 30; a fix that deletes tests must not count as success |

## Pull-request policy

| Case | TC | Request | Expected | Decided |
|------|----|---------|----------|---------|
| R01 |  | Approved by a named reviewer as a draft | allow | allow |
| R02 | TC-16 | Rejected by the reviewer | deny | deny |
| R03 |  | No human decision at all | deny | deny |
| R04 |  | Approved but not a draft | deny | deny |
| R05 |  | Approved with auto-merge on | deny | deny |
| R06 | TC-15 | Approved although the remediation policy denied it | deny | deny |
| R07 |  | Approved by an empty reviewer name | deny | deny |

## Where it fell short

Every case met its ground truth.

## Tuning log

| Date | Change | Effect |
|------|--------|--------|
| 2026-11-13 | E09: `replicas: 2 -> 0` was diagnosed as a configuration error but not reverted, because `replicas` was not treated as a positive quantity. Added replicas, instances, threads and pool to POSITIVE_QUANTITY_KEY_RE in agents/code_agent/reasoning.py, with unit tests. | fix recall 87.5% -> 100.0%; E09 confidence 0.70 -> 0.90, so ECE 0.220 -> 0.207 and Brier 0.063 -> 0.058 |

Re-running after a change is the only way a score here moves; the cases are never edited to fit.

## Threats to validity

* The cases were written by the author of the rules, so a perfect score mostly shows consistency, not generality.
* The Jenkins and metrics evidence is recorded, not live; fusion is real, its inputs are not.
* 15 commits is a small sample; one case moves a rate by about 7 points.
* The one tuning change was made after seeing case E09, so E09 is no longer an unseen case.
* Calibration is measured on 15 points; ECE with 5 bins is coarse at this size.
* The baseline is deliberately naive. It is the strawman H2 names, not a tuned competitor.

> To add a case, append it to `scenarios/remediation_eval/cases.yaml` and re-run.

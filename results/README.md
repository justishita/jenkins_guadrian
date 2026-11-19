# Results

Measured outputs of the Week 8 evaluations. The scores are reported, not asserted: a miss is a finding, and every file
says what its numbers can and cannot show. Regenerate with the commands below.

| File | What it measures | Command | Status |
|---|---|---|---|
| `coordinator_eval.md` / `.json` | The Coordinator's verdict accuracy, how often it acts, and how often it is wrong *and* acts, against baselines (highest confidence, majority vote, each agent alone, P2's noisy-OR fusion). The 13 labelled scenarios plus a seeded simulation of agent behaviour | `python scripts/evaluate_coordinator.py` | done; the simulation models the inputs, it is **not** measured accuracy of the real agents |
| Metrics agent (printed report, `--json` for a file) | Scenario benchmark, severity sweep and **200 random held-out cases** against a threshold baseline (and an LLM baseline with `--llm`) | `python -m scripts.score_metrics_agent --json results/metrics_eval.json` | done; the held-out set found and fixed a real defect (leaks that start late) |
| `e2e_accuracy.md` / `.json` (and `e2e_run1_before_prometheus_fix.*`) | Real faults injected into the real stack, scored against the injector's ground truth: Coordinator and each agent, every miss, wrong-and-actionable | `powershell -File scripts\inject_all_faults.ps1 -Rounds 2`, wait 10-15 minutes, then `python scripts/evaluate_end_to_end.py` | **pending the live run**; the file here, if present, is a one-incident dry run |
| `performance_eval.md` / `.json`, `performance_runs.csv`, `performance_*.svg` | Run time (measured on recorded runs and by replaying recorded evidence and build logs through the real code), and cost per prompt variant (**estimated** from prompt sizes and an assumed price table; no LLM is called). Box plots as SVG | `python scripts/evaluate_performance.py` | done; LLM latency and per-variant accuracy are **not** measured (needs live runs) |
| `guardrails_eval.md` / `.json` (and `guardrails_eval_before_hardening.*`) | The input, output and tool guardrails against an adversarial dataset: content variants, structure variants, obfuscation, abusive personas and benign controls. **Held-out** and dev sets reported separately | `python scripts/evaluate_guardrails.py` | done; held-out 7/33 attacks stopped (21%), 4/17 benign wrongly stopped. The dev set (100%) was tuned against and is only a regression check |
| `remediation_eval.md` / `.json` | The Code & Remediation agent and the policy gate (P3) | `python scripts/evaluate_remediation.py` | P3 |

## Headline numbers so far

- **Fusion beats any single agent** by a wide margin in the simulation (for example 92% against 65% for the best single agent
  in the medium-skill, independent-error condition). The authoritative Coordinator is wrong-and-acting less often than
  highest-confidence and majority vote in all 12 conditions, and than noisy-OR in 7 of 12. It is about 0.6 points *less* accurate
  than plain majority vote: its value is the completeness and confidence gating, not extra accuracy.
- **Shared mistakes are the weak spot**: when agents are misled the same way, wrong-and-acting rises to about 9% on average
  (21% in the worst simulated condition), because agreement then confirms the error.
- **The Metrics agent scores about 99.5-100% on 200 random held-out cases** (against 44-49% for fixed thresholds) with no false
  alarms on healthy cases, after a defect in leak detection was fixed. Simulated faults, so this is generalisation across
  parameters, not realism.
- **One real injected fault so far** (a failing unit test) was diagnosed as `infra_network_failure`, wrongly, and the report was
  still actionable. The live run on all four fault types is what turns that single data point into a measurement.

## Run 1 of the end-to-end evaluation, and what it found (`e2e_run1_before_prometheus_fix.md`)

Ten real fault incidents (clean, one fault per branch): the Coordinator was right **0/10**, and said `infra_network_failure`
every time. The cause was not the models. Prometheus scraped `target_app:8000` but the compose service had been renamed
`target-app`, so Prometheus saw the app as permanently down and the **Metrics agent reported "target down" (about 0.86) for every
incident**. That constant signal outvoted the Jenkins agent even where Jenkins was right (3 of the 10). It was invisible until a
real run exposed it: no scenario test could, because the scenarios feed the agent its own simulated metrics.

Two more findings in the same run, both about agents owned by others, and unaffected by the fix:
- The **Code agent abstained on all 10** (it reaches GitHub and reads the commit, then answers "unknown").
- The **Jenkins agent called unit-test and flaky failures `dependency_regression`** (0.70-0.81), and was right on the compile errors.

Fixed on this side: the scrape address (and a test that every Prometheus target is a compose service). The run has to be repeated
after the fix, because it measured the system with a broken input. Run 1's branches are listed in `fault_branches_run1.txt`
for cleanup.

## Caveat found afterwards: run 2 is confounded for the unit-test and flaky faults (2026-11-19)

While completing the fix -> approval -> pull request -> re-validation check live, Jenkins's test stage was found to **die at test
collection on every branch**, including `main`: the Jenkins container was given the whole `.env` (`env_file`), so the target app
saw the backend's `DATABASE_URL` (`postgresql+asyncpg://...`) and its own settings check raised `ValidationError ... Settings`
before any test ran. So **no test in `test_orders.py` had ever run in this stack's Jenkins**.

What that means for `e2e_accuracy.md` (run 2, Coordinator 4/10):
- `tc01_unit_test` (0/2) and `tc10_flaky` (0/4): the injected assertion and the flaky test never executed; the build failed on the
  collection error instead. These six rows measure how the agents read that error, **not** the injected faults. Treat them as invalid.
- `tc02_compile_syntax` (2/2) fails in a stage before the tests, so it is probably unaffected. `tc02_compile_import` (2/2) fails at
  import/collection, so it may be affected; unconfirmed.
- Fixed in `docker-compose.yml` (Jenkins now receives only the variables its own config reads) and guarded by `tests/test_compose.py`.
  After the fix the same pipeline ran **9 tests green**. The live evaluation should be repeated before quoting a live number
  (`powershell -File scripts\inject_all_faults.ps1 -Rounds 2`, then `evaluate_end_to_end.py --branches results\fault_branches.txt`).

Also found and fixed in the same session: a Jenkins build of a fix branch was reported as a new failure (opening a duplicate incident)
instead of as the re-validation of the fix; the UI's nginx cached the API's address and answered 502 after the API was recreated.

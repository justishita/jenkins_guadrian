# Results

Measured outputs of the Week 8 evaluations. The scores are reported, not asserted: a miss is a finding, and every file
says what its numbers can and cannot show. Regenerate with the commands below.

| File | What it measures | Command | Status |
|---|---|---|---|
| `coordinator_eval.md` / `.json` | The Coordinator's verdict accuracy, how often it acts, and how often it is wrong *and* acts, against baselines (highest confidence, majority vote, each agent alone, P2's noisy-OR fusion). The 13 labelled scenarios plus a seeded simulation of agent behaviour | `python scripts/evaluate_coordinator.py` | done; the simulation models the inputs, it is **not** measured accuracy of the real agents |
| Metrics agent (printed report, `--json` for a file) | Scenario benchmark, severity sweep and **200 random held-out cases** against a threshold baseline (and an LLM baseline with `--llm`) | `python -m scripts.score_metrics_agent --json results/metrics_eval.json` | done; the held-out set found and fixed a real defect (leaks that start late) |
| `e2e_accuracy.md` / `.json` | Real faults injected into the real stack, scored against the injector's ground truth: Coordinator and each agent, every miss, wrong-and-actionable | `powershell -File scripts\inject_all_faults.ps1 -Rounds 2`, wait 10-15 minutes, then `python scripts/evaluate_end_to_end.py` | **pending the live run**; the file here, if present, is a one-incident dry run |
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

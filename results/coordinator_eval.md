# Coordinator evaluation

_Generated 2026-11-14 22:26 UTC by `scripts/evaluate_coordinator.py` (simulation: n=1000 per condition, seed 2026)._

**What the columns mean.** *Accuracy*: the verdict equals the true cause. *Acts*: the report would be actionable, so a fix is proposed. *Right when acting*: of the incidents it acts on, how many were right. **Wrong and acting**: wrong *and* acted on, the dangerous case for guarded remediation. *Acts despite conflict*: of the incidents it acts on, how many carried a flagged conflict. *Acts on ambiguous*: how many of the genuinely ambiguous scenarios it went on to act on (the right answer is none). *ECE / Brier*: how far the confidence is from the true hit rate (0 is perfectly calibrated).

## A. The labelled scenarios

10 labelled cases (of 13; the rest are genuinely ambiguous). Hand-written by the Coordinator's authors: this shows misbehaviour, not general quality.

| Method | Accuracy | Acts | Right when acting | **Wrong and acting** | Acts despite conflict | Acts on ambiguous | ECE | Brier |
|---|---|---|---|---|---|---|---|---|
| `authoritative` | 80% | 50% | 100% | **0%** | 0% | 3/3 | 0.275 | 0.1373 |
| `noisy_or` | 100% | 70% | 100% | **0%** | 0% | 2/3 | 0.2774 | 0.1422 |
| `highest_confidence` | 80% | 90% | 78% | **20%** | 0% | 3/3 | 0.205 | 0.2273 |
| `majority_vote` | 90% | 90% | 89% | **10%** | 22% | 3/3 | 0.215 | 0.2216 |
| `only_jenkins` | 80% | 80% | 88% | **10%** | 0% | 3/3 | 0.17 | 0.206 |
| `only_metrics` | 70% | 60% | 100% | **0%** | 0% | 0/3 | 0.25 | 0.145 |
| `only_code` | 20% | 10% | 0% | **10%** | 0% | 3/3 | 0.235 | 0.1913 |

Misses on the labelled cases:

- `authoritative`: coord-compatible-close: said timeout, truth resource_exhaustion; coord-corroboration-vs-single: said unknown, truth timeout
- `noisy_or`: none
- `highest_confidence`: coord-compatible-close: said timeout, truth resource_exhaustion; coord-corroboration-vs-single: said config_error, truth timeout
- `majority_vote`: coord-compatible-close: said timeout, truth resource_exhaustion
- `only_jenkins`: coord-compatible-close: said timeout, truth resource_exhaustion; coord-metrics-only-signal: said unknown, truth resource_exhaustion
- `only_metrics`: coord-healthy-adequate-metrics: said unknown, truth timeout; coord-no-data-metrics: said unknown, truth timeout; coord-no-double-count: said unknown, truth timeout
- `only_code`: coord-compatible-close: said unknown, truth resource_exhaustion; coord-corroboration-vs-single: said config_error, truth timeout; coord-failed-agent: said unknown, truth timeout; coord-healthy-adequate-metrics: said unknown, truth timeout; coord-metrics-only-signal: said unknown, truth resource_exhaustion; coord-no-data-metrics: said unknown, truth timeout; coord-no-double-count: said unknown, truth timeout; coord-partial-reporting: said unknown, truth timeout

Acted on a genuinely ambiguous case (authoritative): 3 of 3.

## B. Simulation

Condition = agent skill / how errors are distributed / relevance model. Agents answer correctly with probability `skill x relevance` for the true cause, otherwise abstain (60%) or answer wrongly and overconfidently; reported confidence is 0.65-0.95 when right and 0.50-0.85 when wrong, so confidence is only weakly informative. *shared* errors mean a wrong answer is the same wrong cause as the other agents' 60% of the time (overlapping evidence). *flat* relevance gives every agent 0.8 for every cause, so the result does not depend on the proposed relevance weights that P2's fusion also uses.

**This is a simulation of the inputs, not measured accuracy of the real agents.**

### What the numbers say

- The authoritative Coordinator was more accurate than the best single agent in 12 of 12 conditions.
- Versus `highest_confidence`: accuracy more in 6, equal in 0, less in 6 conditions (average -0.1 points); wrong-and-acting lower in 12 of 12.
- Versus `majority_vote`: accuracy more in 0, equal in 0, less in 12 conditions (average -0.6 points); wrong-and-acting lower in 12 of 12.
- Versus `noisy_or`: accuracy more in 6, equal in 0, less in 6 conditions (average +1.4 points); wrong-and-acting lower in 7 of 12.
- Worst case for the authoritative Coordinator: `low/shared/proposed`, where it was wrong *and* went on to act in 21% of incidents.
- Its wrong-and-acting rate averaged 6% when the agents' errors were independent and 9% when they shared a misleading signal.

### `high/independent/proposed`

| Method | Accuracy | Acts | Right when acting | **Wrong and acting** | Acts despite conflict | ECE | Brier |
|---|---|---|---|---|---|---|---|
| `authoritative` | 93% | 92% | 97% | **3%** | 38% | 0.1191 | 0.0622 |
| `noisy_or` | 93% | 92% | 96% | **3%** | 0% | 0.0979 | 0.0622 |
| `highest_confidence` | 91% | 99% | 92% | **8%** | 0% | 0.0783 | 0.0764 |
| `majority_vote` | 93% | 99% | 94% | **6%** | 42% | 0.1432 | 0.0735 |
| `only_jenkins` | 85% | 90% | 94% | **5%** | 0% | 0.1446 | 0.0656 |
| `only_metrics` | 42% | 66% | 64% | **24%** | 0% | 0.0926 | 0.1301 |
| `only_code` | 50% | 70% | 72% | **19%** | 0% | 0.0552 | 0.1129 |

### `high/independent/flat`

| Method | Accuracy | Acts | Right when acting | **Wrong and acting** | Acts despite conflict | ECE | Brier |
|---|---|---|---|---|---|---|---|
| `authoritative` | 96% | 97% | 98% | **2%** | 20% | 0.0857 | 0.0345 |
| `noisy_or` | 93% | 91% | 96% | **4%** | 0% | 0.125 | 0.0819 |
| `highest_confidence` | 96% | 100% | 96% | **4%** | 0% | 0.1083 | 0.0476 |
| `majority_vote` | 96% | 100% | 97% | **3%** | 23% | 0.1711 | 0.0614 |
| `only_jenkins` | 77% | 86% | 90% | **9%** | 0% | 0.1181 | 0.0801 |
| `only_metrics` | 77% | 86% | 90% | **9%** | 0% | 0.1183 | 0.0789 |
| `only_code` | 77% | 85% | 91% | **8%** | 0% | 0.1208 | 0.0732 |

### `high/shared/proposed`

| Method | Accuracy | Acts | Right when acting | **Wrong and acting** | Acts despite conflict | ECE | Brier |
|---|---|---|---|---|---|---|---|
| `authoritative` | 93% | 94% | 96% | **4%** | 34% | 0.1028 | 0.0601 |
| `noisy_or` | 94% | 93% | 97% | **2%** | 0% | 0.0998 | 0.0565 |
| `highest_confidence` | 92% | 100% | 93% | **7%** | 0% | 0.09 | 0.0697 |
| `majority_vote` | 93% | 100% | 94% | **6%** | 38% | 0.1458 | 0.0738 |
| `only_jenkins` | 85% | 90% | 93% | **6%** | 0% | 0.1449 | 0.0665 |
| `only_metrics` | 46% | 66% | 69% | **20%** | 0% | 0.0566 | 0.1149 |
| `only_code` | 53% | 70% | 75% | **18%** | 0% | 0.0602 | 0.1065 |

### `high/shared/flat`

| Method | Accuracy | Acts | Right when acting | **Wrong and acting** | Acts despite conflict | ECE | Brier |
|---|---|---|---|---|---|---|---|
| `authoritative` | 96% | 97% | 97% | **2%** | 25% | 0.0891 | 0.0411 |
| `noisy_or` | 92% | 88% | 95% | **5%** | 0% | 0.1168 | 0.0965 |
| `highest_confidence` | 94% | 100% | 94% | **6%** | 0% | 0.0933 | 0.0582 |
| `majority_vote` | 96% | 100% | 96% | **4%** | 27% | 0.1644 | 0.0644 |
| `only_jenkins` | 73% | 83% | 88% | **10%** | 0% | 0.1106 | 0.0824 |
| `only_metrics` | 76% | 86% | 88% | **10%** | 0% | 0.1191 | 0.0857 |
| `only_code` | 74% | 85% | 87% | **11%** | 0% | 0.1043 | 0.086 |

### `medium/independent/proposed`

| Method | Accuracy | Acts | Right when acting | **Wrong and acting** | Acts despite conflict | ECE | Brier |
|---|---|---|---|---|---|---|---|
| `authoritative` | 85% | 85% | 92% | **6%** | 41% | 0.0949 | 0.0934 |
| `noisy_or` | 86% | 86% | 94% | **6%** | 0% | 0.0902 | 0.092 |
| `highest_confidence` | 85% | 98% | 87% | **13%** | 0% | 0.0504 | 0.0975 |
| `majority_vote` | 86% | 98% | 87% | **12%** | 48% | 0.0856 | 0.1021 |
| `only_jenkins` | 70% | 82% | 86% | **12%** | 0% | 0.0806 | 0.0864 |
| `only_metrics` | 35% | 62% | 57% | **26%** | 0% | 0.1212 | 0.1384 |
| `only_code` | 44% | 66% | 66% | **22%** | 0% | 0.0783 | 0.1243 |

### `medium/independent/flat`

| Method | Accuracy | Acts | Right when acting | **Wrong and acting** | Acts despite conflict | ECE | Brier |
|---|---|---|---|---|---|---|---|
| `authoritative` | 92% | 91% | 96% | **4%** | 32% | 0.0933 | 0.0593 |
| `noisy_or` | 85% | 84% | 89% | **10%** | 0% | 0.0904 | 0.1325 |
| `highest_confidence` | 92% | 99% | 92% | **8%** | 0% | 0.0806 | 0.0703 |
| `majority_vote` | 92% | 99% | 93% | **7%** | 37% | 0.132 | 0.0784 |
| `only_jenkins` | 63% | 79% | 80% | **16%** | 0% | 0.067 | 0.1041 |
| `only_metrics` | 64% | 79% | 82% | **14%** | 0% | 0.0754 | 0.096 |
| `only_code` | 65% | 80% | 81% | **15%** | 0% | 0.0687 | 0.1031 |

### `medium/shared/proposed`

| Method | Accuracy | Acts | Right when acting | **Wrong and acting** | Acts despite conflict | ECE | Brier |
|---|---|---|---|---|---|---|---|
| `authoritative` | 84% | 89% | 89% | **9%** | 39% | 0.0793 | 0.1002 |
| `noisy_or` | 87% | 86% | 93% | **6%** | 0% | 0.0893 | 0.0876 |
| `highest_confidence` | 85% | 98% | 87% | **13%** | 0% | 0.0523 | 0.1005 |
| `majority_vote` | 85% | 98% | 86% | **14%** | 45% | 0.1044 | 0.105 |
| `only_jenkins` | 73% | 84% | 87% | **10%** | 0% | 0.1136 | 0.0821 |
| `only_metrics` | 37% | 63% | 59% | **26%** | 0% | 0.1084 | 0.1389 |
| `only_code` | 41% | 64% | 64% | **23%** | 0% | 0.0816 | 0.1227 |

### `medium/shared/flat`

| Method | Accuracy | Acts | Right when acting | **Wrong and acting** | Acts despite conflict | ECE | Brier |
|---|---|---|---|---|---|---|---|
| `authoritative` | 90% | 92% | 94% | **6%** | 32% | 0.086 | 0.0705 |
| `noisy_or` | 86% | 81% | 91% | **7%** | 0% | 0.114 | 0.1351 |
| `highest_confidence` | 91% | 99% | 92% | **8%** | 0% | 0.0833 | 0.0739 |
| `majority_vote` | 91% | 99% | 91% | **9%** | 37% | 0.1309 | 0.0846 |
| `only_jenkins` | 62% | 77% | 81% | **15%** | 0% | 0.0752 | 0.0968 |
| `only_metrics` | 65% | 79% | 82% | **14%** | 0% | 0.0832 | 0.0947 |
| `only_code` | 62% | 79% | 79% | **17%** | 0% | 0.0638 | 0.107 |

### `low/independent/proposed`

| Method | Accuracy | Acts | Right when acting | **Wrong and acting** | Acts despite conflict | ECE | Brier |
|---|---|---|---|---|---|---|---|
| `authoritative` | 74% | 81% | 83% | **14%** | 42% | 0.1025 | 0.1343 |
| `noisy_or` | 76% | 80% | 85% | **12%** | 0% | 0.0502 | 0.1376 |
| `highest_confidence` | 75% | 97% | 77% | **22%** | 0% | 0.0704 | 0.1484 |
| `majority_vote` | 75% | 97% | 77% | **22%** | 52% | 0.0388 | 0.1503 |
| `only_jenkins` | 57% | 75% | 76% | **18%** | 0% | 0.0565 | 0.1097 |
| `only_metrics` | 28% | 56% | 49% | **28%** | 0% | 0.1445 | 0.1435 |
| `only_code` | 33% | 60% | 55% | **27%** | 0% | 0.1247 | 0.1408 |

### `low/independent/flat`

| Method | Accuracy | Acts | Right when acting | **Wrong and acting** | Acts despite conflict | ECE | Brier |
|---|---|---|---|---|---|---|---|
| `authoritative` | 80% | 84% | 89% | **10%** | 36% | 0.0802 | 0.1069 |
| `noisy_or` | 75% | 71% | 84% | **11%** | 0% | 0.0976 | 0.173 |
| `highest_confidence` | 80% | 97% | 83% | **17%** | 0% | 0.0423 | 0.1205 |
| `majority_vote` | 81% | 97% | 84% | **16%** | 45% | 0.0633 | 0.12 |
| `only_jenkins` | 48% | 67% | 72% | **19%** | 0% | 0.062 | 0.1093 |
| `only_metrics` | 48% | 69% | 70% | **20%** | 0% | 0.0609 | 0.1185 |
| `only_code` | 48% | 68% | 70% | **21%** | 0% | 0.0617 | 0.1223 |

### `low/shared/proposed`

| Method | Accuracy | Acts | Right when acting | **Wrong and acting** | Acts despite conflict | ECE | Brier |
|---|---|---|---|---|---|---|---|
| `authoritative` | 66% | 81% | 74% | **21%** | 39% | 0.0784 | 0.1779 |
| `noisy_or` | 70% | 77% | 80% | **15%** | 0% | 0.0408 | 0.1507 |
| `highest_confidence` | 68% | 95% | 71% | **27%** | 0% | 0.1178 | 0.1763 |
| `majority_vote` | 67% | 95% | 70% | **28%** | 48% | 0.0861 | 0.1777 |
| `only_jenkins` | 54% | 72% | 75% | **18%** | 0% | 0.0333 | 0.1155 |
| `only_metrics` | 26% | 57% | 46% | **31%** | 0% | 0.1666 | 0.1582 |
| `only_code` | 33% | 61% | 53% | **28%** | 0% | 0.1361 | 0.1485 |

### `low/shared/flat`

| Method | Accuracy | Acts | Right when acting | **Wrong and acting** | Acts despite conflict | ECE | Brier |
|---|---|---|---|---|---|---|---|
| `authoritative` | 79% | 86% | 84% | **13%** | 37% | 0.0822 | 0.1147 |
| `noisy_or` | 74% | 76% | 80% | **15%** | 0% | 0.0859 | 0.1873 |
| `highest_confidence` | 80% | 97% | 83% | **17%** | 0% | 0.0654 | 0.1158 |
| `majority_vote` | 79% | 97% | 81% | **18%** | 45% | 0.0915 | 0.1243 |
| `only_jenkins` | 49% | 72% | 68% | **23%** | 0% | 0.0733 | 0.1342 |
| `only_metrics` | 46% | 68% | 69% | **21%** | 0% | 0.0625 | 0.1193 |
| `only_code` | 51% | 71% | 72% | **20%** | 0% | 0.0681 | 0.1174 |

## Limits

- The labelled scenarios are few and author-written; the simulation's agent model is an assumption, not data.
- Real agents' errors are neither independent nor as simple as this; the live fault run (`scripts/evaluate_end_to_end.py`) is the check on real behaviour, and its first result was wrong where this model would have predicted a right answer.
- `noisy_or` shares its relevance weights with the simulation under the *proposed* model; read it under *flat* for a fairer comparison.

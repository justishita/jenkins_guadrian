# Agentic design patterns: what we use, and what we compared it with

**Owner:** P2 (Week 8). Covers course criteria 1 (shared state, other messaging architectures) and 4 (hierarchical, parallel,
sequential, evaluated against alternatives). Every claim says whether it is **measured** (a number from this repo), a **design
argument** (reasoning, not an experiment) or **not tested**.

## The shape of the system

A failed Jenkins build opens an incident. Three investigation agents look at it from different sides (the Jenkins agent reads the
build, the Metrics agent reads Prometheus, the Code agent reads the commit). Each writes **evidence** to a shared store. A
Coordinator waits for them, fuses what they found into one report, and the remediation engine decides whether anything may be
done about it, behind a policy gate and a human approval.

## Patterns in use

| Pattern | Where | What it does here |
|---|---|---|
| **Hierarchical** (supervisor over workers) | `coordinator/app.py`, `coordinator/agent.py` | The Coordinator (a Google ADK `BaseAgent`) is the only component that dispatches work and decides. The three agents are workers; they never call each other. |
| **Parallel** (fan-out, fan-in) | `Coordinator._dispatch_all`: `asyncio.gather` over the agents, then one `synthesize` | The three agents run at the same time; the Coordinator waits for all (bounded by a timeout), then fuses once. A late agent triggers a report *revision*, not a restart. |
| **Sequential** (a fixed chain of steps) | inside each worker | Jenkins agent: rules, then LLM reasoning, then evidence (CrewAI `Process.sequential`). Code agent: `fetch_context -> analyze -> reason -> ...` (CrewAI `Flow`, `@start` / `@listen`). Metrics agent: plan, query, detect (a `Flow`). The remediation pipeline is also strictly ordered: propose, policy, approval, pull request, re-validation. |
| **Blackboard** (shared state) | `common/blackboard.py`, `common/evidence_store.py`, `coordinator/stores.py` | Agents write evidence and hints to an incident-scoped store; the Coordinator reads it. This is the mandatory shared-state pattern. |

Frameworks: CrewAI for the three investigation agents, Google ADK for the Coordinator, A2A between them
(`docs/decisions.md`, "Framework boundary"). No other agent framework is used.

## Three messaging mechanisms, three jobs (criterion 1)

The assignment asks for the blackboard to be mandatory and for other messaging architectures to be tried. We use three, each for
what it is good at:

| Mechanism | Carries | Why this one |
|---|---|---|
| **Blackboard / evidence store** | what an agent *concluded*: hypotheses, supporting evidence, hints between agents | durable, incident-scoped, replayable, one place to audit |
| **A2A (agent-to-agent protocol)** | *commands and acknowledgements*: "investigate this incident", "evidence is ready" | a standard, framework-neutral boundary between CrewAI and ADK (`docs/a2a_spike_findings.md`: a round trip is about 30-40 ms on localhost) |
| **RabbitMQ events** | *something happened*: incident created, approval needed | decouples the API from the agents; an agent that is down misses nothing |

## Alternatives, and what we know about each

### Sequential Coordinator instead of parallel (measured)

If the Coordinator ran the three agents one after another, an incident would wait for the **sum** of their times instead of about
the slowest. From 32 real incidents with all three dispatch times recorded (`results/performance_eval.md`, part E,
`python scripts/evaluate_performance.py --stack`):

| | median | p95 | mean |
|---|---|---|---|
| sequential (sum of three) | 35.4 s | 77.2 s | 36.2 s |
| parallel (slowest of three) | 28.8 s | 40.5 s | 24.5 s |
| time saved | 6.4 s | 35.7 s | 11.7 s |

Parallel saves about a fifth at the median and a third on average. It is bounded by the slowest agent, and one agent usually
dominates: the Metrics agent was slowest in 24 of the 32. So parallelism is worth having but is not a large win here; the real
cost of an investigation is whichever agent is waiting on a slow tool.
Caveat: the times were recorded while the agents ran in parallel, so a sequential run might differ a little.

A second argument for parallel is independence (design argument): the fusion treats agents as separate witnesses. In a sequence,
a later agent could read an earlier agent's conclusion and agree with it, which would make "two agents agree" mean less.

### Peer-to-peer messaging instead of a blackboard (design argument, **not tested**)

We did not build a peer-to-peer version, so there is no measurement to compare. The reasons we did not:

- Fusion needs every agent's conclusion in one place. With peer-to-peer, either every agent sends to every other (N x N channels
  and N copies of the truth) or one of them becomes a hub, which is the blackboard again.
- Replay and audit. Because all evidence sits in one store, the recorded evidence of 80 incidents can be fed back through the real
  `synthesize` and gives the same verdict every time (`results/performance_eval.md`, part B: **measured**, 400 replays, 0 unstable).
  That check is only possible because the shared state is the single source of what each agent said.
- Failure isolation. An agent that dies leaves a missing entry; the Coordinator reports the investigation as incomplete and the
  remediation engine will not act on an incomplete report.

The cost of the blackboard is coupling: every agent depends on the evidence schema, and a store read error can look like a missing
agent (fixed: `MergedEvidenceStore.read_errors`). Peer-to-peer would be worth trying if agents needed to *negotiate* (for
example, the Metrics agent asking the Code agent a follow-up question). Today none does: the Jenkins agent posts hints to the
blackboard and the others read them, which is the lighter version of that.

### One agent instead of several (measured, with a caveat)

- **In simulation** (`results/coordinator_eval.md`) the authoritative Coordinator was more accurate than the best single agent in
  12 of 12 conditions (for example 92% against 65%). The simulation models the agents, so this says what fusion *can* do.
- **On real injected faults** (`results/e2e_accuracy.md`) the Coordinator got 4/10 and the Jenkins agent alone also got 4/10. The
  real Metrics agent mostly abstained and the Code agent abstained every time, so fusion had nothing to add. It did not make
  things worse (no case where a correct agent was overruled), but the live run does **not** show fusion beating the best single
  agent. The honest summary is: fusion is only as good as the agents' independent signal.

### Deeper hierarchy (design argument)

A second tier of sub-coordinators (for example one per evidence family) would add a layer to audit and to time out for no gain with
three workers. Not built.

### An LLM deciding the order or the fusion (design argument)

The Coordinator is deterministic on purpose: the same evidence always gives the same report (measured, above), and a remediation
decision should not depend on a model's mood. LLMs are used inside the workers, where wording and reasoning help, not in the
decision of who is right.

## Where the patterns hurt

- Parallel dispatch means one slow agent holds the report back until the timeout; the late-revision path softens this.
- The blackboard is a single point where a bad entry misleads everyone. Untrusted text is screened and wrapped before it reaches
  a model (`docs/guardrails.md`), and every write is redacted.
- Hierarchical control puts the Coordinator on the critical path; if it is down no report is made (it restarts by itself and
  recovers unfinished incidents).

## Reproduce

```bash
python scripts/evaluate_performance.py --stack      # parts A-E, writes results/performance_*.{md,json,csv,svg}
python scripts/evaluate_coordinator.py              # fusion against single agents, simulated
python scripts/evaluate_end_to_end.py --branches results/fault_branches.txt   # live faults (needs the stack)
```

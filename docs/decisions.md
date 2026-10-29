# Project Decisions & Team Contract

**Project Name:** AI-Powered DevOps Incident Investigation & Remediation  
**Repository:** `devops-ai-agents`  

All team members must adhere to these decisions to maintain integration compatibility across services.

---

## Core Technical Decisions

| # | Decision Area | Agreed Standard | Rationale & Impact |
|---|---|---|---|
| **1** | **Agent Framework** | **CrewAI** for Jenkins, Metrics and Code agents; **Google ADK** for the Coordinator | CrewAI for the three investigation agents. Google ADK for the Coordinator. No other agentic framework permitted (no LangChain, no LangGraph). |
| **2** | **LLM & Client Wrapper** | **Gemini Flash** via `common/llm_client.py` | Cost-effective free tier. CrewAI reaches it through a `BaseLLM` adapter (`CrewLLMAdapter`), ADK uses Gemini natively. Own free key per person. |
| **3** | **Failure-Type Taxonomy** | **Unified Enum** (see section below) | Canonical classification taxonomy across all investigation agents, evidence store entries, and coordinator reports. |
| **4** | **Time Standard** | **UTC, ISO-8601 with 'Z'** (`2026-10-01T09:30:00Z`) | Absolute requirement. Metrics correlation, log parsing windowing, and incident sequence synthesis will break if non-UTC timestamps are used. |
| **5** | **Git Workflow** | **Feature Branches + PR to `develop`** | `main` is protected (production-ready). `develop` is the primary integration branch. Individual work occurs on `p1/...`, `p2/...`, `p3/...` feature branches. All PRs require at least 1 team review before merging. **Never force-push `main` or `develop`.** |
| **6** | **Folder Ownership & Boundaries** | Strict ownership by team role (see section below) | Prevents merge conflicts and unauthorized cross-service coupling. Cross-agent imports are strictly prohibited. |
| **7** | **Context Sharing** | **Blackboard** (`common/blackboard.py`) is mandatory | Append-only, versioned shared state. RabbitMQ stays for events. `TRIGGER_MODE=a2a` (default) or `rabbitmq` for the messaging comparison in evaluation. |
| **8** | **Inter-Agent Protocol** | **A2A** (`a2a-sdk`) | Coordinator <-> agent communication via A2A protocol. Each agent is its own container and virtualenv, because CrewAI and ADK dependency pins can conflict. |
| **9** | **Tool Protocol** | **MCP** servers under `mcp_servers/` | Official `mcp` SDK (FastMCP). Agents never import tool code directly. |

---

## 1. Unified Failure-Type Taxonomy (Enum)

All agents must restrict their hypothesis classifications to the following enum values:

* `code_test_failure` — Unit/integration test assertion failures.
* `build_compilation_failure` — Syntax errors, compilation failures, missing module imports.
* `dependency_regression` — Upstream library updates or third-party package incompatibilities.
* `timeout` — Build stage, test execution, or network connection timeouts.
* `resource_exhaustion` — Out-Of-Memory (OOM) kills, CPU throttling, high disk utilization.
* `infra_network_failure` — DNS failures, connection refused, upstream service/dependency outages.
* `config_error` — Malformed configuration files (`settings.yaml`, `deploy.env`), missing environment variables.
* `auth_failure` — 401/403 HTTP errors, credential failures, expired access tokens.
* `flaky_test` — Non-deterministic test passes/failures without underlying commit code changes.
* `deployment_failure` — Deployment script or health-check verification failures post-build.
* `unknown` — Unclassified or ambiguous root causes.

---

## 2. Directory Ownership & Service Boundaries

Each top-level directory has a single clear owner. **Do not edit files outside your assigned directory without team notification and PR approval.**

### Framework boundary

LangGraph and LangChain are both gone. `grep -rn "langchain\|langgraph"` over `agents/`,
`coordinator/`, `common/`, `backend/` and `mcp_servers/` returns nothing: the shared
`LLMClient` no longer depends on LangChain either.

The only agentic frameworks are the two the course requires. The investigation agents
use CrewAI - the Jenkins agent as an LLM `Crew`, the code and metrics agents as
deterministic `Flow`s - and the Coordinator is a Google ADK `BaseAgent`. A2A is the
boundary between them.


---

## 3. Coordinator: which report decides, and what is still unsigned (Week 7, P2)

**Authoritative report.** `coordinator/synthesis.py` (agreement bonus, conflict penalty, the confidence
floor of 0.5 shared with the policy) produces the `IncidentReport` the remediation engine and the UI read.
`actionable` requires `complete` (no missing or failed agent) and the confidence floor.

**Advisory report.** P2's fusion (`coordinator/fusion.py`, `FusedReport`: noisy-OR combination, per-agent
relevance, gated negative evidence) is a second method that never decides anything: the live Coordinator
(`coordinator/app.py`) does not run it and no gate reads it. It runs only from `scripts/synthesize_incident.py`
(and the unwired listener below), where it is audited as `fused_report_completed`. It exists for the Week 8
comparison (`docs/fusion_method_comparison.md`).

**Status: awaiting sign-off from P3.** The values below are P2's proposal; none has been approved, and the
code says so (`signed_off=False`, `trust_proposed_relations=False`). Until they are signed off, a pair of types
that is not signed-off compatible is treated as *conflicting*, which is the safe direction (it lowers
confidence, it never raises it).

| Setting | Proposed value | Meaning |
|---|---|---|
| `min_relevance_for_negative` | 0.5 | an agent may vote *against* a type only if its relevance to that type is at least this |
| `min_coverage` | 0.6 | "found nothing" counts as absence only at or above this declared coverage |
| `negative_strength` | 0.15 | penalty per negative vote = this x the agent's relevance |
| `discount_cap` | 0.30 | total negative discount on one type never exceeds this |
| `conflict_margin` | 0.10 | types within this of the leader, and not compatible, conflict |
| `trust_proposed_relations` | off | unsigned relations are ignored |

Agent relevance (`AGENT_RELEVANCE`; 1.0 = speaks fully to that cause):

| Failure type | jenkins_agent | metrics_agent | code_agent |
|---|---|---|---|
| code_test_failure | 1.0 | 0.3 | 1.0 |
| build_compilation_failure | 1.0 | 0.2 | 1.0 |
| dependency_regression | 1.0 | 0.2 | 0.8 |
| timeout | 0.9 | 0.8 | 0.2 |
| resource_exhaustion | 0.6 | 1.0 | 0.2 |
| infra_network_failure | 0.7 | 0.9 | 0.1 |
| config_error | 0.9 | 0.3 | 0.8 |
| auth_failure | 1.0 | 0.1 | 0.3 |
| flaky_test | 0.8 | 0.2 | 0.5 |
| deployment_failure | 1.0 | 0.5 | 0.4 |

Proposed compatible pairs (`TYPE_RELATIONS`; may both be true at once, for conflict detection only):
`resource_exhaustion`+`timeout`, `infra_network_failure`+`timeout`, `dependency_regression`+`build_compilation_failure`,
`config_error`+`deployment_failure`, `code_test_failure`+`flaky_test`. Every other pair is unclassified (conflicting).

**To sign off:** P3 approves or changes each row/pair; the change is then `signed_off=True` in `fusion.py` and
this table is updated in the same commit.

**Superseded.** `coordinator/service.py` and `coordinator/a2a_client.py` (an A2A listener that waits for all
agents) are not wired into `docker-compose.yml`: the dispatcher in `coordinator/app.py` is the live path
(`TRIGGER_MODE=a2a`, bearer-authenticated agent servers). The listener's endpoint has no authentication and its
pending timers are in memory, so it must not be enabled as is.

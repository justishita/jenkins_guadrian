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

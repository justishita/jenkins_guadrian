 # Project Decisions & Team Contract

**Project Name:** AI-Powered DevOps Incident Investigation & Remediation  
**Repository:** `devops-ai-agents`  

All team members must adhere to these decisions to maintain integration compatibility across services.

---

## Core Technical Decisions

| # | Decision Area | Agreed Standard | Rationale & Impact |
|---|---|---|---|
| **1** | **Agent Framework** | **LangGraph** (primary) | Ensures seamless integration, standard state management, and unified execution across all three agents (Jenkins, Metrics, Code) and the Coordinator. |
| **2** | **LLM & Client Wrapper** | **Gemini Flash** via `common/llm_client.py` | Cost-effective free tier. Standardized behind `LLMClient` to abstract provider details, enforce rate limiting, and support offline testing. **Each team member must generate and use their own free API key in `.env` to prevent shared quota depletion.** |
| **3** | **Failure-Type Taxonomy** | **Unified Enum** (see section below) | Canonical classification taxonomy across all investigation agents, evidence store entries, and coordinator reports. |
| **4** | **Time Standard** | **UTC, ISO-8601 with 'Z'** (`2026-10-01T09:30:00Z`) | Absolute requirement. Metrics correlation, log parsing windowing, and incident sequence synthesis will break if non-UTC timestamps are used. |
| **5** | **Git Workflow** | **Feature Branches + PR to `develop`** | `main` is protected (production-ready). `develop` is the primary integration branch. Individual work occurs on `p1/...`, `p2/...`, `p3/...` feature branches. All PRs require at least 1 team review before merging. **Never force-push `main` or `develop`.** |
| **6** | **Folder Ownership & Boundaries** | Strict ownership by team role (see section below) | Prevents merge conflicts and unauthorized cross-service coupling. Cross-agent imports are strictly prohibited. |

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
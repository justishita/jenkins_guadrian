# Copilot Instructions: JenkinsGUardians

Project: **JenkinsGUardians: AI-Powered DevOps Incident Investigation & Remediation**

## Architecture

A failed Jenkins pipeline sends a webhook to the FastAPI backend. The Coordinator assigns an incident ID and dispatches work through RabbitMQ to three independent LLM agents:

- Jenkins Investigation
- Metrics Investigation
- Code & Remediation

Agents never call, import, or otherwise depend on one another. Each agent writes only its findings and confidence to the shared Evidence Store, using the common Pydantic JSON schema. The Coordinator synthesizes the evidence, then runs the result through an OPA/Conftest policy check. A human must approve before a draft GitHub PR is created. Jenkins re-validates the proposed remediation afterward.

Keep these ownership boundaries explicit in code. Agent-to-agent communication is prohibited; coordination and synthesis belong to the Coordinator and the shared Evidence Store.

## Stack

Python 3.11, FastAPI, CrewAI (Jenkins, Metrics, Code agents), Google ADK (Coordinator only), A2A protocol (a2a-sdk) for Coordinator<->agent communication, MCP (official `mcp` SDK) for tools, Blackboard (common/blackboard.py) for shared state, RabbitMQ (pika or aio-pika) for events, Postgres, pydantic v2, OpenTelemetry, pytest, Docker Compose.

## Engineering Rules

- NEVER import langchain*, langgraph*, llama_index, autogen or any agentic framework other than crewai and google-adk.
- Agents never call other agents (they only read/write the Blackboard and answer A2A requests from the Coordinator).
- Every agent final output is a pydantic model (CrewAI output_pydantic / ADK output_schema).
- Every Crew/Agent/Task has monitoring callbacks from common.monitoring.
- Every text from logs, commits or users passes common.guardrails.screen_untrusted_text before reaching an LLM.
- Tools are exposed only through MCP servers in mcp_servers/.
- The Blackboard is append-only and each agent writes only its own namespace.
- Add type hints to all functions, methods, and variables where practical; public APIs and boundary-facing data must be fully typed.
- Use Pydantic v2 models for all data crossing a process, service, queue, persistence, API, or agent boundary. Keep the shared Evidence Store JSON schema canonical and version changes deliberately.
- Represent timestamps as timezone-aware UTC values and serialize them as ISO-8601.
- Never log or send secrets. Before any text reaches an LLM or the database, pass it through `common.redaction.redact()`.
- Give every external call an explicit timeout and retry transient failures with bounded backoff. Do not retry non-transient errors blindly.
- Use structured logging; never use `print()`.
- Keep functions small and focused, and handle failures explicitly.
- Add or update pytest tests for every module. Cover boundary validation, failure paths, retries/timeouts, and redaction where applicable.
- Never hard-code URLs, credentials, tokens, or other environment-specific values. Read configuration from environment variables and validate it at startup.
- Agents must not import from another agent's package. Put shared contracts and utilities in neutral shared modules, not inside an agent package.
- Require human approval before creating or submitting any GitHub pull request. Generated changes should be drafts, and Jenkins re-validation must remain part of the flow.

## Failure Taxonomy

Use this enum vocabulary exactly when classifying incidents; do not invent aliases or silently map an unknown category to a more specific one:

```python
from enum import StrEnum


class FailureTaxonomy(StrEnum):
    CODE_TEST_FAILURE = "code_test_failure"
    BUILD_COMPILATION_FAILURE = "build_compilation_failure"
    DEPENDENCY_REGRESSION = "dependency_regression"
    TIMEOUT = "timeout"
    RESOURCE_EXHAUSTION = "resource_exhaustion"
    INFRA_NETWORK_FAILURE = "infra_network_failure"
    CONFIG_ERROR = "config_error"
    AUTH_FAILURE = "auth_failure"
    FLAKY_TEST = "flaky_test"
    DEPLOYMENT_FAILURE = "deployment_failure"
    UNKNOWN = "unknown"
```

Use `unknown` when evidence is insufficient to select another category.

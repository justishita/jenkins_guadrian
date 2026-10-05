# Common Contracts and Utilities

**Owner:** P3 owns the shared evidence schema and redaction contract. Shared
LLM access is implemented in `llm_client.py`; agents consume shared contracts
and utilities here.

## LLM client

`LLMClient.chat()` is an async provider-neutral chat entry point. It returns
the provider's chat response (normally a LangChain `AIMessage`), or the
validated Pydantic/dictionary result when `response_schema` is supplied.
Messages are redacted before they are sent. When a schema is supplied, the
client requests structured output and makes one repair call if parsing or
validation fails. Optional LangChain tools can be passed through `tools`.

The default provider is Gemini Flash (`GEMINI_API_KEY`,
`GEMINI_MODEL=gemini-2.5-flash`). Set `LLM_PROVIDER=openai` to use
`OPENAI_API_KEY` and `OPENAI_MODEL=gpt-4o-mini`. `LLM_TIMEOUT_SECONDS`
controls the call timeout, and `LLM_REQUESTS_PER_MINUTE` configures the
token-bucket limit (10 requests/minute by default). Calls retry HTTP 429 and
5xx errors with bounded exponential backoff. Set `LLM_OFFLINE=1` to disable
provider calls and raise `LLMUnavailable`, allowing callers to use fallbacks.

Per-call structured logs contain only the provider, redacted prompt character
count and SHA-256 hash, token usage when available, latency, and safe error
metadata. Prompt text is never logged. Tests can use `MockLLMClient` with a
sequence of scripted values or exceptions.

## Evidence store

`EvidenceStore` defines async `write(evidence)`, `read_all(incident_id)`, and
`read(incident_id, agent)` operations. `FileEvidenceStore` is the temporary
worker backend and writes redacted, validated evidence under
`./data/evidence/<incident_id>/<agent>.json`. Writes replace a document
atomically and increment its stored `version` on each retry for the same
incident and agent. The Jenkins-agent container bind-mounts this directory so
evidence remains available on the host. `PostgresEvidenceStore` implements
the same interface for deployments that opt into PostgreSQL storage.
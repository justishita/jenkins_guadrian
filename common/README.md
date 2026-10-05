# Common Contracts and Utilities

**Owner:** P3 owns the shared evidence schema, the audit trail and the redaction
contract. Shared LLM access is implemented in `llm_client.py`; agents consume
shared contracts and utilities here.

## LLM client

`LLMClient.chat()` is an async provider-neutral chat entry point. It returns
the provider's chat response (normally a LangChain `AIMessage`), or the
validated Pydantic/dictionary result when `response_schema` is supplied.
Messages are redacted before they are sent. When a schema is supplied, the
client requests structured output and makes one repair call if parsing or
validation fails. Optional LangChain tools can be passed through `tools`.

Gemini Flash is the provider (`GEMINI_API_KEY`,
`GEMINI_MODEL=gemini-2.5-flash`); `LLM_PROVIDER` defaults to and currently
accepts `gemini`. `LLM_TIMEOUT_SECONDS` controls the call timeout, and
`LLM_REQUESTS_PER_MINUTE` configures the token-bucket limit (10 requests/minute
by default). Calls retry HTTP 429 and 5xx errors with bounded exponential
backoff. Set `LLM_OFFLINE=1` to disable provider calls and raise
`LLMUnavailable`, allowing callers to use fallbacks.

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

## Evidence schema

`evidence_schema.json` is the canonical contract: one document per agent per
incident, which is how the three agents stay independent of each other. It is
generated from `common/models.py` and must never be hand-edited:

```bash
make schema                                         # regenerate and verify
python scripts/generate_evidence_schema.py --check  # fail if stale
```

`tests/common/test_evidence_schema.py` fails if the file drifts from the model,
if the taxonomy enum diverges, or if the wire version changes without the other
agents being updated with it.

`evidence_schema_stub.json` is the Week 1/2 placeholder this schema replaces.
Both describe the same `0.1-stub` wire format, so documents written against
either remain valid; the metrics agent still loads the stub and can be pointed
at the canonical file through `EVIDENCE_SCHEMA_PATH`. Bumping the version to
`1.0` is a coordinated change across all three agents in one PR.

## Audit trail

`audit.py` records what happened while an incident was handled - agent runs,
tool calls, policy verdicts, approvals and outcomes. `FileAuditLog` appends JSON
lines under `./data/audit/<incident_id>.jsonl` for local runs and the agent
containers; `PostgresAuditLog` writes the `audit_events` table created by the
migrations in `migrations/`. `build_audit_log()` picks between them and wraps
the result in `BestEffortAuditLog`, so losing the trail degrades traceability
without failing the investigation - the dropped record is logged.

`AuditRecord` redacts its own `summary` and `payload`, including values stored
under secret-looking keys such as `api_key` or `authorization`, so a caller
cannot forget. Records are append-only: a later outcome is a later record, never
an update.

# Metrics Agent

**Owner:** P2. Investigates operational metrics and contributes evidence through the shared contract.

## Pipeline (Week 2)

`incident.created` → `consumer.py` → `Investigator` (`investigator.py`):

1. `window.py` – investigation window around the failure `timestamp` plus a preceding baseline (UTC).
2. `planner.py` – `QueryPlanner` picks queries from `queries.py` (`CatalogPlanner` today; an LLM planner can implement the same protocol later).
3. `prometheus_tool.py` – the only code that talks to Prometheus (`query_range`, timeouts, bounded retry, typed errors).
4. `sanity.py` – empty / too few samples / stale / out-of-range data means "cannot conclude", never "anomaly".
5. `anomaly.py` – deterministic detectors: robust z-score spike, sustained increase, availability.
6. `hypotheses.py` – maps detections to the exact failure taxonomy; no anomaly → `insufficient_evidence` / `unknown`.
7. `evidence.py` + `store.py` – builds the shared `common.models.Evidence` and writes it to the shared Evidence Store (`FileEvidenceStore`) through an `EvidenceWriter`.

Investigation errors become `status: failed` evidence; the consumer never dies on a bad incident.
The agent waits `INVESTIGATION_SETTLE_SECONDS` (default 30) after a very recent failure so Prometheus has scraped it.

## Audit trail

Each investigation is recorded through the shared `common.audit` layer as `agent_started`, one `tool_call`
per Prometheus query, `hypothesis_formed`, `evidence_written`, and `agent_completed` (or `agent_failed`).
`tool_call` records carry the time the query was issued (`issued_at` in the tool call's `args`, also visible
in the evidence), not the time they were written. The trail is PostgreSQL when `DATABASE_URL` is set,
otherwise JSON lines under `data/audit/`. A broken audit log never blocks the investigation.

To run the Postgres round-trip test: `TEST_DATABASE_URL=postgresql+asyncpg://user:pw@localhost:5432/db pytest tests/metrics_agent/test_audit_postgres.py`.

## Run

`docker compose up -d metrics-agent`. Evidence lands in `./data/evidence/<incident_id>/metrics_agent.json`
(bind mount shared with the other agents, so the Coordinator reads everything with `read_all(incident_id)`).
Config is env-driven; see `config.py`.

## Demo

`python scripts/inject_faults.py slow|cpu|leak|healthy` (enable fault → traffic → wait for metrics → publish incident).
Leave ~6 minutes between runs so one fault is not inside the next incident window.

# Metrics Agent

**Owner:** P2. Investigates operational metrics and contributes evidence through the shared contract.

## Pipeline

`incident.created` → `consumer.py` → `Investigator` (`investigator.py`). The shape is
`per-metric detection → per-metric evidence → cross-metric correlation → hypotheses`; there is
no combined inference engine.

1. `window.py` – investigation window around the failure `timestamp` plus a preceding baseline (UTC).
2. `planner.py` – `QueryPlanner` picks queries from `queries.py` (`CatalogPlanner` today; an LLM planner can implement the same protocol later).
3. `prometheus_tool.py` – the only code that talks to Prometheus (`query_range`, timeouts, bounded retry, typed errors).
4. `sanity.py` – empty / too few samples / stale / out-of-range data means "cannot conclude", never "anomaly". It also scores data **quality** (sample coverage, dropped non-finite values).
5. `anomaly.py` – per-metric detectors (robust z-score spike, sustained increase, availability). A metric must stay elevated for **2 consecutive samples**, so one noisy scrape is never a cause. Each detection reports strength, persistence and how close to the failure it was (including "recovered N s before the failure").
6. `correlation.py` – a few explicit rules turn findings into candidate causes (see below).
7. `confidence.py` + `hypotheses.py` – score each candidate cause, write the reasoning into the hypothesis, rank them; no anomaly → `insufficient_evidence` / `unknown`.
8. `evidence.py` + `store.py` – builds the shared `common.models.Evidence` and writes it to the shared Evidence Store (`FileEvidenceStore`) through an `EvidenceWriter`.

Investigation errors become `status: failed` evidence; the consumer never dies on a bad incident.
The agent waits `INVESTIGATION_SETTLE_SECONDS` (default 30) after a very recent failure so Prometheus has scraped it.

## Correlation rules

1. Anomalous metrics that point to the same failure type form **one** cause; the others corroborate the strongest (CPU + memory → one `resource_exhaustion` hypothesis).
2. Elevated latency that **coincides in time** with a resource-exhaustion anomaly is a **symptom**: cited as supporting evidence ("coincides with elevated latency_p95"), not reported as a second, competing cause. Timing is judged on each metric's most recent contiguous elevated stretch (within 60 s); an earlier, recovered anomaly stays a separate, lower-confidence cause and does not corroborate.
3. `contradicting_evidence` comes **only from relevant catalog metrics** (those mapping to the same failure type) that were measured and stayed normal, e.g. normal memory against a CPU claim. Metrics that could not be measured neither support nor contradict.
4. When the target is down, metrics that stopped reporting are noted as consistent with the outage.

## Confidence

Deterministic and reflects evidence quality, not just magnitude (`confidence.py`):

```
core       = 0.10 + 0.35*strength + 0.20*persistence + 0.25*quality
proximity  = 0.35 + 0.65*closeness
confidence = clamp(core * proximity + corroboration, 0, 0.97)
```

`strength`: how far past its threshold (0..1). `persistence`: 2 consecutive samples = 0.33, 4+ = 1.0. `quality`: sanity coverage. `closeness`: `1/(1+gap/120s)` where gap is the time from the last elevated sample to the failure (1.0 at the failure, 0.5 two minutes before), applied to the whole score so a huge old spike cannot outrank a moderate one at the failure. `corroboration`: +0.10 per supporting metric (max +0.15), −0.05 per relevant metric that stayed normal (max −0.10). With no anomaly, confidence is `0.2 × data quality`; with no usable data, 0. Each anomalous metric's evidence item records its `confidence_inputs`.

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

`python scripts/inject_faults.py slow|cpu|leak|healthy` (enable fault → traffic → wait for metrics → publish incident). Add `--via-webhook` to send it through agent-api's `/webhooks/jenkins` (secret read from `WEBHOOK_SHARED_SECRET`, only ever sent to a local host) so the backend, the incidents table and the queue are exercised too.
Leave ~6 minutes between runs so one fault is not inside the next incident window.

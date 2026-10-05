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
7. `evidence.py` + `store.py` – evidence record shaped like `common/evidence_schema_stub.json`, validated and written through an `EvidenceWriter` (local JSON adapter now; swap for P3's store later).

Investigation errors become `status: failed` evidence; the consumer never dies on a bad incident.
The agent waits `INVESTIGATION_SETTLE_SECONDS` (default 30) after a very recent failure so Prometheus has scraped it.

## Run

`docker compose up -d metrics-agent`. Evidence lands in `/app/evidence/<incident_id>.metrics_agent.json`
(volume `metrics_agent_evidence`). Config is env-driven; see `config.py`.

## Demo

`python scripts/inject_faults.py slow|cpu|leak|healthy` (enable fault → traffic → wait for metrics → publish incident).
Leave ~6 minutes between runs so one fault is not inside the next incident window.

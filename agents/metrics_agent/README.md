# Metrics Agent

**Owner:** P2. Investigates operational metrics and contributes evidence through the shared contract.

Week 1 status: foundation only.

- `prometheus_tool.py` – standalone `query()` / `query_range()` client with timeout, bounded retry, typed errors. The agent never makes HTTP calls itself.
- `consumer.py` – consumes `incident.created` from its own queue `metrics_agent.incident.created` and logs "ready for investigation".
- `models.py`, `config.py` – typed boundary models and env-validated settings.

Run: `python -m agents.metrics_agent` (needs `PROMETHEUS_URL`, `RABBITMQ_URL`) or `docker compose up metrics-agent`.
Investigation logic (PromQL selection, anomaly detection, evidence) is Week 2.

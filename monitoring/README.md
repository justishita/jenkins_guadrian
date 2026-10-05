# Monitoring

**Owner:** P2. Prometheus + Grafana for the target app and Jenkins.

| Service | URL (compose default) | Notes |
|---|---|---|
| Prometheus | http://localhost:9090 | Targets: `prometheus`, `target_app` (`/metrics`), `jenkins` (`/prometheus/`) |
| Grafana | http://localhost:3000 | Login from `GRAFANA_ADMIN_USER/PASSWORD` in `.env`; datasource + dashboards are provisioned from this folder |
| target_app | http://localhost:8001 | Fault endpoints `/slow`, `/cpu`, `/leak` need `ORDERS_ENABLE_*=true` in `.env` |

If host ports 8080/9090 are taken, remap them in a local, gitignored `docker-compose.override.yml` (use `ports: !override`).

Jenkins is scraped unauthenticated (`useAuthenticatedEndpoint: false` in `jenkins/jenkins.yaml`, local dev only). The plugin refreshes every 120 s, so Jenkins series appear a couple of minutes after start, and `default_jenkins_builds_*` only exist after a job has run.

## Fault injection → known-good PromQL

Verified against the running stack.

| Fault | Trigger | PromQL | Observed |
|---|---|---|---|
| Latency | `GET /slow?delay=2` | `sum by (path)(http_request_duration_seconds_sum{path="/slow"}) / sum by (path)(http_request_duration_seconds_count{path="/slow"})` | ~2.0 s avg |
| p95 latency | any traffic | `histogram_quantile(0.95, sum by (le)(rate(http_request_duration_seconds_bucket[5m])))` | ~7 ms idle |
| CPU | `GET /cpu?seconds=4` | `rate(process_cpu_seconds{job="target_app"}[1m])` | spike above ~0.002 idle |
| CPU total | | `process_cpu_seconds{job="target_app"}` | monotonic counter-like gauge |
| Memory leak | `GET /leak` (1 MiB per call) | `process_memory_bytes{job="target_app"}` | ~80 MB after 20 calls |
| Request count | | `sum(http_requests_total{job="target_app"})` | |
| 5xx rate | | `sum(rate(http_requests_total{status=~"5.."}[1m])) or vector(0)` | 0 when healthy |
| Jenkins queue | | `jenkins_queue_size_value` | 0 idle |
| Jenkins builds | after a build runs | `default_jenkins_builds_last_build_result_ordinal`, `default_jenkins_builds_last_build_duration_milliseconds`, `default_jenkins_builds_success_build_count`, `default_jenkins_builds_failed_build_count` | not yet verified (no builds ran) |

Note: `process_cpu_seconds` is exported as a gauge by target_app, so `rate()` works but `increase()`-style counter semantics (resets) do not apply.

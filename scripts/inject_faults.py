#!/usr/bin/env python3
"""Reproducible fault injection for the Metrics agent. Owner: P2.

Effects (read before running):
  * drives load against target_app (/slow, /cpu, /leak or /health),
  * a leak scenario permanently grows target_app memory until it is restarted,
  * publishes ONE synthetic `incident.created` message to RabbitMQ.

Order matters, so the incident timestamp matches the real failure:
  1. preflight: the fault endpoint must be enabled (ORDERS_ENABLE_* in .env),
  2. generate fault traffic,
  3. wait until Prometheus actually shows the change (polled, not a blind sleep),
  4. publish `incident.created` with timestamp = end of the fault traffic,
  5. the Metrics agent investigates, comparing the incident window to its baseline.

Usage:
  python scripts/inject_faults.py slow|cpu|leak|healthy [--prometheus-url URL] ...
"""

import argparse
import json
import logging
import math
import os
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

logger = logging.getLogger("inject_faults")

MIB = 1024 * 1024
POLL_INTERVAL_SECONDS = 5
SIGNAL_TIMEOUT_SECONDS = 180
HTTP_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True)
class Scenario:
    name: str
    probe_path: str | None  # 404 here means the fault is not enabled on target_app
    enable_hint: str
    drive: Callable[[httpx.Client], None]
    signal_query: str | None
    # (current value, value before the fault) -> has the fault become visible?
    signal_visible: Callable[[float, float | None], bool]


def _repeat(path: str, times: int, pause: float = 0.0) -> Callable[[httpx.Client], None]:
    def drive(client: httpx.Client) -> None:
        for _ in range(times):
            response = client.get(path)
            if response.status_code == 413:
                logger.warning("target_app reports its configured memory-growth limit is reached")
                return
            response.raise_for_status()
            time.sleep(pause)

    return drive


SCENARIOS: dict[str, Scenario] = {
    "slow": Scenario(
        name="slow",
        probe_path="/slow?delay=0",
        enable_hint="ORDERS_ENABLE_SLOW=true",
        drive=_repeat("/slow?delay=2", times=12),
        signal_query=(
            "histogram_quantile(0.95, sum by (le) "
            '(rate(http_request_duration_seconds_bucket{job="target_app"}[1m])))'
        ),
        signal_visible=lambda now, _before: now > 0.5,
    ),
    "cpu": Scenario(
        name="cpu",
        probe_path="/cpu?seconds=0",
        enable_hint="ORDERS_ENABLE_CPU=true",
        drive=_repeat("/cpu?seconds=4", times=6),
        signal_query='rate(process_cpu_seconds{job="target_app"}[1m])',
        signal_visible=lambda now, _before: now > 0.03,
    ),
    "leak": Scenario(
        name="leak",
        probe_path=None,  # /leak has no side-effect-free probe; the first real call reveals a 404
        enable_hint="ORDERS_ENABLE_LEAK=true",
        drive=_repeat("/leak", times=20, pause=0.5),
        signal_query='process_memory_bytes{job="target_app"}',
        signal_visible=lambda now, before: before is not None and now - before > 5 * MIB,
    ),
    "healthy": Scenario(
        name="healthy",
        probe_path="/health",
        enable_hint="(nothing to enable)",
        drive=_repeat("/health", times=20, pause=1.0),
        signal_query=None,
        signal_visible=lambda _now, _before: True,
    ),
}


def query_value(prometheus: httpx.Client, promql: str) -> float | None:
    """Instant query returning the first sample's value, or None if there is no data."""
    response = prometheus.get("/api/v1/query", params={"query": promql})
    response.raise_for_status()
    result = response.json()["data"]["result"]
    if not result:
        return None
    value = float(result[0]["value"][1])
    return value if math.isfinite(value) else None


def preflight(target: httpx.Client, scenario: Scenario) -> None:
    if scenario.probe_path is None:
        return
    status = target.get(scenario.probe_path).status_code
    if status == 404:
        sys.exit(f"{scenario.name} fault is disabled on target_app. Set {scenario.enable_hint} in .env and restart it.")
    if status >= 400:
        sys.exit(f"target_app probe {scenario.probe_path} returned HTTP {status}")


def wait_for_signal(prometheus: httpx.Client, scenario: Scenario, before: float | None) -> None:
    if scenario.signal_query is None:
        return
    deadline = time.monotonic() + SIGNAL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        current = query_value(prometheus, scenario.signal_query)
        logger.info("waiting for metrics to change", extra={"current": current, "before": before})
        if current is not None and scenario.signal_visible(current, before):
            logger.info("fault is visible in Prometheus", extra={"value": current})
            return
        time.sleep(POLL_INTERVAL_SECONDS)
    sys.exit("fault never became visible in Prometheus; refusing to publish a misleading incident")


def publish_incident(rabbit: httpx.Client, fault_end: datetime) -> str:
    incident_id = str(uuid.uuid4())
    build_number = int(time.time() % 100_000)
    message = {
        "incident_id": incident_id,
        "job_name": "target-app/main",
        "build_number": build_number,
        "build_url": f"http://jenkins.invalid/job/target-app/job/main/{build_number}/",
        "branch": "main",
        "git_commit": "fault-injection",
        "failed_stage": "Deploy",
        "remediation_attempt": 0,
        "timestamp": fault_end.isoformat(),
        "received_at": datetime.now(timezone.utc).isoformat(),
    }
    response = rabbit.post(
        "/api/exchanges/%2F/incidents/publish",
        json={
            "properties": {"delivery_mode": 2},
            "routing_key": "incident.created",
            "payload": json.dumps(message),
            "payload_encoding": "string",
        },
    )
    response.raise_for_status()
    if not response.json().get("routed"):
        sys.exit("incident was not routed to any queue; is the metrics-agent running?")
    return incident_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("scenario", choices=sorted(SCENARIOS))
    parser.add_argument("--target-url", default=os.getenv("ORDERS_URL", "http://localhost:8001"))
    parser.add_argument("--prometheus-url", default=os.getenv("PROMETHEUS_HOST_URL", "http://localhost:9090"))
    parser.add_argument("--rabbitmq-url", default=os.getenv("RABBITMQ_MANAGEMENT_URL", "http://localhost:15672"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    scenario = SCENARIOS[args.scenario]
    auth = (os.getenv("RABBITMQ_USER", "rabbit"), os.getenv("RABBITMQ_PASSWORD", "rabbit_password"))
    with (
        httpx.Client(base_url=args.target_url, timeout=HTTP_TIMEOUT_SECONDS) as target,
        httpx.Client(base_url=args.prometheus_url, timeout=HTTP_TIMEOUT_SECONDS) as prometheus,
        httpx.Client(base_url=args.rabbitmq_url, timeout=HTTP_TIMEOUT_SECONDS, auth=auth) as rabbit,
    ):
        preflight(target, scenario)  # 1. fault enabled?
        before = query_value(prometheus, scenario.signal_query) if scenario.signal_query else None
        logger.info("injecting fault", extra={"scenario": scenario.name})
        scenario.drive(target)  # 2. generate fault traffic
        fault_end = datetime.now(timezone.utc)
        wait_for_signal(prometheus, scenario, before)  # 3. wait until metrics change
        incident_id = publish_incident(rabbit, fault_end)  # 4. only now announce the incident
    logger.info("incident published; the Metrics agent now investigates", extra={"incident_id": incident_id})
    logger.info(
        "read the result with: docker compose exec metrics-agent cat /app/evidence/%s.metrics_agent.json",
        incident_id,
    )


if __name__ == "__main__":
    main()

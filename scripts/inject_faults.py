#!/usr/bin/env python3
"""Reproducible fault injection for the Metrics agent. Owner: P2.

Effects (read before running):
  * drives load against target_app (/slow, /cpu, /leak or /health),
  * a leak scenario permanently grows target_app memory until it is restarted,
  * publishes ONE synthetic `incident.created` (to RabbitMQ, or through the webhook with --via-webhook).

Order matters, so the incident timestamp matches the real failure:
  1. preflight: the fault endpoint must be enabled (ORDERS_ENABLE_* in .env),
  2. generate fault traffic,
  3. wait until Prometheus actually shows the change (polled, not a blind sleep),
  4. publish `incident.created` with timestamp = end of the fault traffic,
  5. the Metrics agent investigates, comparing the incident window to its baseline.

Usage:
  python scripts/inject_faults.py slow|cpu|leak|healthy [--prometheus-url URL] ...

By default the incident goes straight to RabbitMQ. With --via-webhook it is POSTed to
agent-api's /webhooks/jenkins instead (secret from WEBHOOK_SHARED_SECRET in the environment or
.env, only ever sent to a local host), so the backend, the incidents table and the queue are all
exercised, and every agent receives it. The Jenkins and Code agents cannot investigate a fake
build and will log a failure for it; that is expected.
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
from pathlib import Path
from urllib.parse import urlparse

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


LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]", "host.docker.internal"})
WEBHOOK_PATH = "/webhooks/jenkins"


def failure_fields(fault_end: datetime) -> dict[str, object]:
    """The Jenkins-failure fields shared by both publishing paths; `timestamp` is the fault's end."""
    build_number = int(time.time() % 100_000)
    return {
        "job_name": "target-app/main",
        "build_number": build_number,
        "build_url": f"http://jenkins.invalid/job/target-app/job/main/{build_number}/",
        "branch": "main",
        "git_commit": "fault-injection",
        "failed_stage": "Deploy",
        "remediation_attempt": 0,
        "timestamp": fault_end.isoformat(),
    }


def ensure_local(url: str, allow_remote: bool) -> None:
    """The webhook secret must never leave the machine unless the caller says so explicitly."""
    host = urlparse(url).hostname or ""
    if host not in LOCAL_HOSTS and not allow_remote:
        sys.exit(f"refusing to send the webhook secret to non-local host {host!r}; pass --allow-remote to override")


def load_webhook_secret(env_file: Path = Path(".env")) -> str:
    """WEBHOOK_SHARED_SECRET from the environment, else from a local `.env`. Never printed."""
    secret = os.getenv("WEBHOOK_SHARED_SECRET", "")
    if not secret and env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "WEBHOOK_SHARED_SECRET":
                secret = value.strip().strip("\"'")
    if not secret:
        sys.exit("WEBHOOK_SHARED_SECRET is not set (environment or .env); it is required for --via-webhook")
    return secret


def publish_via_webhook(api: httpx.Client, secret: str, fault_end: datetime) -> str:
    """POST the failure to the real backend webhook, so backend, database and queue are all exercised."""
    response = api.post(WEBHOOK_PATH, json=failure_fields(fault_end), headers={"X-Webhook-Token": secret})
    if response.status_code == httpx.codes.UNAUTHORIZED:
        sys.exit("the webhook rejected the token (HTTP 401): check WEBHOOK_SHARED_SECRET matches agent-api's")
    if response.status_code not in (httpx.codes.OK, httpx.codes.ACCEPTED):
        sys.exit(f"the webhook answered HTTP {response.status_code}; is agent-api healthy?")
    return str(response.json()["incident_id"])


def publish_incident(rabbit: httpx.Client, fault_end: datetime) -> str:
    """Publish `incident.created` straight to RabbitMQ (bypasses the backend and database)."""
    incident_id = str(uuid.uuid4())
    message = {
        "incident_id": incident_id,
        **failure_fields(fault_end),
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
    parser.add_argument(
        "--via-webhook",
        action="store_true",
        help="announce the incident through agent-api's /webhooks/jenkins (backend + database + queue)"
        " instead of publishing straight to RabbitMQ",
    )
    parser.add_argument("--api-url", default=os.getenv("AGENT_API_URL", "http://localhost:8000"))
    parser.add_argument("--allow-remote", action="store_true", help="allow --api-url to be a non-local host")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    scenario = SCENARIOS[args.scenario]
    secret = ""
    if args.via_webhook:
        ensure_local(args.api_url, args.allow_remote)
        secret = load_webhook_secret()
    rabbit_auth = (os.getenv("RABBITMQ_USER", "rabbit"), os.getenv("RABBITMQ_PASSWORD", "rabbit_password"))
    with (
        httpx.Client(base_url=args.target_url, timeout=HTTP_TIMEOUT_SECONDS) as target,
        httpx.Client(base_url=args.prometheus_url, timeout=HTTP_TIMEOUT_SECONDS) as prometheus,
        httpx.Client(base_url=args.rabbitmq_url, timeout=HTTP_TIMEOUT_SECONDS, auth=rabbit_auth) as rabbit,
        httpx.Client(base_url=args.api_url, timeout=HTTP_TIMEOUT_SECONDS) as api,
    ):
        preflight(target, scenario)  # 1. fault enabled?
        before = query_value(prometheus, scenario.signal_query) if scenario.signal_query else None
        logger.info("injecting fault", extra={"scenario": scenario.name})
        scenario.drive(target)  # 2. generate fault traffic
        fault_end = datetime.now(timezone.utc)
        wait_for_signal(prometheus, scenario, before)  # 3. wait until metrics change
        # 4. only now announce the incident
        if args.via_webhook:
            incident_id = publish_via_webhook(api, secret, fault_end)
        else:
            incident_id = publish_incident(rabbit, fault_end)
    logger.info("incident published; the Metrics agent now investigates", extra={"incident_id": incident_id})
    logger.info("evidence: ./data/evidence/%s/metrics_agent.json", incident_id)
    logger.info("audit trail: ./data/audit/%s.jsonl (when the file trail is in use)", incident_id)
    if args.via_webhook:
        logger.info("the Jenkins and Code agents also received it; they fail on a fake build, which is expected")


if __name__ == "__main__":
    main()

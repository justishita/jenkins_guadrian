#!/usr/bin/env python3
"""
Test script to send fake Jenkins failure events to the Agent API webhook.
Verifies:
1. Authentication with X-Webhook-Token
2. Event ingestion and incident creation (202 Accepted)
3. Idempotency on repeated submissions (200 OK with same incident_id)
4. RabbitMQ queue/exchange state inspection via Management API
"""

from datetime import datetime, timezone
import json
import os
import sys
import requests
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

API_URL = os.getenv("AGENT_API_URL", "http://localhost:8000")
WEBHOOK_ENDPOINT = f"{API_URL}/webhooks/jenkins"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SHARED_SECRET", "a8f9c2d1e3f4b5a6c7d8e9f0a1b2c3d4")
RABBITMQ_MANAGEMENT_URL = os.getenv("RABBITMQ_MANAGEMENT_URL", "http://localhost:15672")
RABBITMQ_USER = os.getenv("RABBITMQ_USER", "rabbit")
RABBITMQ_PASSWORD = os.getenv("RABBITMQ_PASSWORD", "rabbit_password")


import time

def create_sample_payload(build_num: int | None = None) -> dict:
    if build_num is None:
        build_num = int(time.time() % 100000)
    return {
        "job_name": "target-app/main",
        "build_number": build_num,
        "build_url": f"http://localhost:8080/job/target-app/job/main/{build_num}/",
        "branch": "main",
        "git_commit": "a1b2c3d4e5f67890abcdef1234567890abcdef12",
        "failed_stage": "Test",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "incident_id": None,
        "remediation_attempt": 0,
    }


def send_webhook(payload: dict, label: str):
    headers = {
        "Content-Type": "application/json",
        "X-Webhook-Token": WEBHOOK_SECRET,
    }
    print(f"\n--- {label} ---")
    print(f"POST {WEBHOOK_ENDPOINT}")
    try:
        response = requests.post(WEBHOOK_ENDPOINT, json=payload, headers=headers, timeout=5)
        print(f"Status Code: {response.status_code}")
        print(f"Response:    {json.dumps(response.json(), indent=2)}")
        return response.json()
    except Exception as e:
        print(f"Error contacting API: {e}")
        return None


def check_rabbitmq_queues():
    print(f"\n--- Checking RabbitMQ Queues via Management API ({RABBITMQ_MANAGEMENT_URL}) ---")
    try:
        url = f"{RABBITMQ_MANAGEMENT_URL}/api/queues"
        resp = requests.get(url, auth=(RABBITMQ_USER, RABBITMQ_PASSWORD), timeout=5)
        if resp.status_code == 200:
            queues = resp.json()
            if not queues:
                print("No queues found in default virtual host yet (agents will declare them on startup).")
            for q in queues:
                print(f"Queue: {q.get('name')} | Messages Ready: {q.get('messages_ready', 0)} | Total: {q.get('messages', 0)}")
        else:
            print(f"Failed to query RabbitMQ Management API (HTTP {resp.status_code})")
    except Exception as e:
        print(f"RabbitMQ Management API not reachable: {e}")


def print_curl_command(payload: dict):
    print("\n--- Equivalent Curl Command ---")
    payload_str = json.dumps(payload)
    print(
        f"curl -X POST {WEBHOOK_ENDPOINT} \\\n"
        f"  -H 'Content-Type: application/json' \\\n"
        f"  -H 'X-Webhook-Token: {WEBHOOK_SECRET}' \\\n"
        f"  -d '{payload_str}'"
    )


def main():
    build_num = int(sys.argv[1]) if len(sys.argv) > 1 else None
    payload = create_sample_payload(build_num)
    print(f"Testing with build #{payload['build_number']}...")

    # 1. First Call: Expect 202 Accepted
    resp1 = send_webhook(payload, "Call 1: First Attempt (Incident Creation)")

    # 2. Second Call: Expect 200 OK (Idempotency)
    resp2 = send_webhook(payload, "Call 2: Second Attempt (Idempotency Check)")

    # Verify ID match
    id1 = resp1.get("incident_id") if resp1 else None
    id2 = resp2.get("incident_id") if resp2 else None

    if id1 and id2 and id1 == id2:
        print("\n[SUCCESS] Idempotency confirmed: both calls returned identical incident_id:", id1)
    else:
        print("\n[WARNING] Incident IDs did not match or request failed.")

    # 3. Check RabbitMQ Queues
    check_rabbitmq_queues()

    # 4. Print curl command
    print_curl_command(payload)


if __name__ == "__main__":
    main()

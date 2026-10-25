"""Pytest configuration and shared fixtures for Jenkins agent tests.

Provides:
- Fully offline testing environment (LLM_OFFLINE=1, respx HTTP mocks, MockLLMClient)
- In-memory evidence store
- Fake Jenkins API responses and sample failure logs
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import respx

from agents.jenkins_agent.tools.jenkins_client import ConsoleText, JenkinsClient
from common.evidence_store import InMemoryEvidenceStore
from common.llm_client import MockLLMClient
from common.models import Evidence, FailureTaxonomy, IncidentCreatedEvent


# ---------------------------------------------------------------------------
# Offline Environment Setup
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def offline_env(monkeypatch: pytest.MonkeyPatch):
    """Enforce offline mode for all unit tests so no real LLM calls or Jenkins calls occur."""
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("JENKINS_URL", "http://jenkins-fake:8080")
    monkeypatch.setenv("JENKINS_USER", "fake-admin")
    monkeypatch.setenv("JENKINS_API_TOKEN", "fake-token-12345")


# ---------------------------------------------------------------------------
# In-Memory Evidence Store Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def in_memory_evidence_store() -> InMemoryEvidenceStore:
    """Provide a thread-safe, non-filesystem evidence store."""
    return InMemoryEvidenceStore()


# ---------------------------------------------------------------------------
# Mock LLM Client Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_llm_client() -> MockLLMClient:
    """Provide a MockLLMClient with scripted responses for offline testing."""
    responses = [
        {
            "failure_type": "code_test_failure",
            "summary": "Mock LLM failure diagnosis",
            "confidence": 0.85,
            "root_cause_hypotheses": [],
            "recommended_next_steps": ["Inspect unit test output"],
        }
    ]
    return MockLLMClient(responses=responses)


# ---------------------------------------------------------------------------
# Fake Jenkins JSON Responses
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_build_info() -> dict[str, Any]:
    """Standard Jenkins build JSON dict for build #42."""
    return {
        "number": 42,
        "result": "FAILURE",
        "building": False,
        "timestamp": 1729881600000,
        "duration": 45000,
        "url": "http://jenkins-fake:8080/job/target-app/job/main/42/",
        "actions": [
            {
                "_class": "hudson.plugins.git.util.BuildData",
                "lastBuiltRevision": {
                    "SHA1": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
                    "branch": [{"name": "origin/main"}],
                },
            }
        ],
    }


@pytest.fixture
def fake_stage_summary() -> dict[str, Any]:
    """Workflow-API pipeline stage description."""
    return {
        "stages": [
            {"id": "1", "name": "Checkout", "status": "SUCCESS", "durationMillis": 1200},
            {"id": "2", "name": "Build", "status": "SUCCESS", "durationMillis": 5400},
            {"id": "3", "name": "Test", "status": "FAILED", "durationMillis": 12300},
            {"id": "4", "name": "Deploy", "status": "NOT_EXECUTED", "durationMillis": 0},
        ]
    }


@pytest.fixture
def fake_test_report() -> dict[str, Any]:
    """Jenkins testReport/api/json payload with 1 failing and 1 passing test."""
    return {
        "failCount": 1,
        "skipCount": 0,
        "totalCount": 25,
        "duration": 12.34,
        "suites": [
            {
                "name": "tests.test_cart",
                "cases": [
                    {
                        "name": "test_calculate_total",
                        "className": "tests.test_cart",
                        "status": "FAILED",
                        "duration": 0.12,
                        "errorDetails": "AssertionError: expected $25.00 but got $0.00",
                        "errorStackTrace": "Traceback (most recent call last):\n  File 'test_cart.py', line 45\nAssertionError",
                    },
                    {
                        "name": "test_empty_cart",
                        "className": "tests.test_cart",
                        "status": "PASSED",
                        "duration": 0.05,
                    },
                ],
            }
        ],
    }


@pytest.fixture
def fake_build_history() -> list[dict[str, Any]]:
    """Build history across the last 5 builds."""
    return [
        {"number": 42, "result": "FAILURE", "building": False, "duration": 45000},
        {"number": 41, "result": "SUCCESS", "building": False, "duration": 42000},
        {"number": 40, "result": "SUCCESS", "building": False, "duration": 41000},
        {"number": 39, "result": "FAILURE", "building": False, "duration": 44000},
        {"number": 38, "result": "SUCCESS", "building": False, "duration": 40000},
    ]


# ---------------------------------------------------------------------------
# Sample Logs Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_compiler_error_log() -> str:
    """Sample build console log demonstrating compilation failure."""
    return """
[Pipeline] stage (Build)
[Pipeline] sh
+ javac -d bin src/main/java/com/app/*.java
src/main/java/com/app/PaymentGateway.java:42: error: cannot find symbol
    symbol:   class SecureTransactionToken
    location: package com.security
1 error
[Pipeline] }
[Pipeline] // stage
ERROR: Build step failed with exit code 1
Finished: FAILURE
"""


@pytest.fixture
def sample_pytest_failure_log() -> str:
    """Sample build console log demonstrating pytest assertion failure."""
    return """
[Pipeline] stage (Test)
[Pipeline] sh
+ pytest -q --tb=short
FAILED tests/test_orders.py::test_calculate_tax - AssertionError: assert 10.5 == 12.0
=========================== short test summary info ===========================
FAILED tests/test_orders.py::test_calculate_tax - AssertionError: assert 10.5 == 12.0
1 failed, 49 passed in 4.12s
[Pipeline] }
[Pipeline] // stage
Finished: FAILURE
"""


@pytest.fixture
def sample_timeout_log() -> str:
    """Sample build console log demonstrating pipeline timeout."""
    return """
[Pipeline] stage (Integration Tests)
[Pipeline] timeout
Cancelling nested steps due to timeout after 15 minutes
Sending interrupt signal to process
Terminated
[Pipeline] }
ERROR: Execution timed out after 15m
Finished: ABORTED
"""


@pytest.fixture
def sample_flaky_log() -> str:
    """Sample build console log demonstrating flaky test failure."""
    return """
[Pipeline] stage (Test)
FAILED tests/test_redis.py::test_connection_pool - ConnectionResetError: connection lost
=========================== short test summary info ===========================
FAILED tests/test_redis.py::test_connection_pool - ConnectionResetError: connection lost
1 failed, 20 passed in 1.15s
Finished: FAILURE
"""


@pytest.fixture
def sample_clean_log() -> str:
    """Sample build console log demonstrating green successful build."""
    return """
[Pipeline] stage (Build)
[Pipeline] sh
+ pytest
50 passed in 3.21s
[Pipeline] stage (Deploy)
Successfully deployed to staging
Finished: SUCCESS
"""


# ---------------------------------------------------------------------------
# Mock Jenkins Client and Respx Routes Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_jenkins_http(
    fake_build_info: dict[str, Any],
    fake_stage_summary: dict[str, Any],
    fake_test_report: dict[str, Any],
    fake_build_history: list[dict[str, Any]],
    sample_pytest_failure_log: str,
):
    """Respx mock context pre-configured for standard Jenkins endpoints."""
    with respx.mock(base_url="http://jenkins-fake:8080", assert_all_called=False) as respx_mock:
        # Build API
        respx_mock.get("/job/target-app/job/main/42/api/json").mock(
            return_value=httpx.Response(200, json=fake_build_info)
        )
        # Console text
        respx_mock.get("/job/target-app/job/main/42/consoleText").mock(
            return_value=httpx.Response(200, text=sample_pytest_failure_log)
        )
        # Stage summary (wfapi)
        respx_mock.get("/job/target-app/job/main/42/wfapi/describe").mock(
            return_value=httpx.Response(200, json=fake_stage_summary)
        )
        # Test report
        respx_mock.get("/job/target-app/job/main/42/testReport/api/json").mock(
            return_value=httpx.Response(200, json=fake_test_report)
        )
        # Build history
        respx_mock.get("/job/target-app/job/main/api/json").mock(
            return_value=httpx.Response(200, json={"builds": fake_build_history})
        )
        # Build trigger
        respx_mock.post("/job/target-app/job/main/build").mock(
            return_value=httpx.Response(201)
        )
        yield respx_mock


@pytest.fixture
def fake_incident_event() -> IncidentCreatedEvent:
    """Standard IncidentCreatedEvent for testing."""
    return IncidentCreatedEvent(
        incident_id=uuid4(),
        job_name="target-app/main",
        build_number=42,
        build_url="http://jenkins-fake:8080/job/target-app/job/main/42/",
        branch="main",
        git_commit="a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
        failed_stage="Test",
        timestamp=datetime.now(timezone.utc),
        remediation_attempt=0,
    )

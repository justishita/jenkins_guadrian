"""The 0.5 confidence floor is the same number in three places; they must agree at the boundary.

**Owner:** P2 (Week 6). The floor appears in the agents' reportable threshold and ``IncidentReport.actionable``
(``coordinator.synthesis.MIN_REPORTABLE_CONFIDENCE``), the fused report's ``LOW_CONFIDENCE`` block, and the
Rego ``min_confidence`` the policy layer enforces. If one drifts, a fix could be proposed that policy then
refuses (annoying), or - worse - one layer could accept what another would refuse. These tests pin them to
the same value and check the real policy at both sides of the line.

The Rego checks need the OPA CLI (``OPA_BIN`` or ``opa`` on PATH); ``python scripts/run_policy_tests.py`` runs
them through Docker.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator.synthesis import MIN_REPORTABLE_CONFIDENCE

REPO_ROOT = Path(__file__).resolve().parents[2]
REGO = (REPO_ROOT / "policies" / "remediation.rego").read_text(encoding="utf-8")


def _opa() -> str | None:
    return os.environ.get("OPA_BIN") or shutil.which("opa")


requires_opa = pytest.mark.skipif(_opa() is None, reason="set OPA_BIN or put opa on PATH")


def evaluate(document: dict) -> dict:
    completed = subprocess.run(
        [_opa(), "eval", "--format", "json", "--stdin-input", "-d", str(REPO_ROOT / "policies"), "data.jenkinsguardians.remediation"],
        input=json.dumps(document),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return json.loads(completed.stdout)["result"][0]["expressions"][0]["value"]


def proposal(confidence: float) -> dict:
    return {
        "incident_id": "11111111-1111-4111-8111-111111111111",
        "failure_type": "dependency_regression",
        "confidence": confidence,
        "attempt": 0,
        "changed_files": [
            {"path": "target_app/requirements.txt", "status": "modified", "additions": 1, "deletions": 1, "patch": "@@ -4 +4 @@\n-httpx==0.28.1\n+httpx==0.99.0"}
        ],
    }


def test_the_python_floor_and_the_rego_floor_are_the_same_number() -> None:
    match = re.search(r"^min_confidence\s*:=\s*([0-9.]+)", REGO, re.MULTILINE)
    assert match is not None, "min_confidence not found in policies/remediation.rego"
    assert float(match.group(1)) == MIN_REPORTABLE_CONFIDENCE


@requires_opa
def test_the_policy_allows_exactly_at_the_floor() -> None:
    verdict = evaluate(proposal(MIN_REPORTABLE_CONFIDENCE))
    assert verdict["allow"] is True, verdict.get("deny")


@requires_opa
def test_the_policy_refuses_just_below_the_floor() -> None:
    verdict = evaluate(proposal(MIN_REPORTABLE_CONFIDENCE - 0.01))
    assert verdict["allow"] is False
    assert any("below the required" in message for message in verdict["deny"])


@requires_opa
@pytest.mark.parametrize("confidence", [0.0, 0.2, 0.49])
def test_nothing_below_the_floor_gets_through(confidence: float) -> None:
    assert evaluate(proposal(confidence))["allow"] is False

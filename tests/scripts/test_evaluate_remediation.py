"""Tests for ``scripts/evaluate_remediation.py``, the Week 8 evaluation.

**Owner:** P3. The scores themselves are reported, not asserted, except the safety
properties: those are requirements, so a regression in any of them fails here.
"""

from __future__ import annotations

import pytest

from scripts.demo_p3 import find_opa
from scripts.evaluate_remediation import blame_latest_commit, evaluate


def test_the_baseline_blames_even_an_empty_commit() -> None:
    kind, edits = blame_latest_commit({"files": []})
    assert kind == "code_test_failure"
    assert edits == []


def test_the_baseline_reverts_every_changed_line() -> None:
    kind, edits = blame_latest_commit(
        {"files": [{"path": "target_app/requirements.txt", "patch": "-httpx==0.28.1\n+httpx==0.99.0\n+extra==1.0"}]}
    )
    assert kind == "dependency_regression"
    assert edits == sorted(
        [("target_app/requirements.txt", "httpx==0.99.0", "httpx==0.28.1"), ("target_app/requirements.txt", "extra==1.0", "")]
    )


@pytest.mark.skipif(find_opa() is None, reason="opa binary not installed (python scripts/get_opa.py)")
@pytest.mark.asyncio
async def test_safety_properties_hold_on_the_whole_evaluation_set() -> None:
    result = await evaluate(find_opa())
    summary = result["summary"]

    assert summary["agent"]["false_blame_rate"] == 0.0  # TC-12, TC-14
    assert summary["agent"]["unnecessary_fix_rate"] == 0.0
    assert summary["agent"]["fix_precision"] == 1.0  # every proposed fix is the minimal one
    assert summary["agent"]["generated_fixes_allowed_by_policy"] == 1.0
    assert summary["policy"]["unsafe_blocked"] == 1.0  # TC-15 and every guardrail
    assert summary["policy"]["safe_allowed"] == 1.0
    assert summary["policy"]["pull_request_accuracy"] == 1.0
    assert summary["policy"]["fails_closed_without_opa"] is True
    assert summary["secrets"] == {"cases": 1, "leaks": 0}  # TC-20
    assert "## Tuning log" in result["markdown"]

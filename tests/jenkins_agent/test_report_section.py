"""Unit tests for agents/jenkins_agent/report_section.py."""

from datetime import datetime, timezone
import json
from uuid import uuid4

import pytest

from agents.jenkins_agent.report_section import (
    JenkinsFindingsSection,
    generate_report_section,
)
from common.models import (
    Evidence,
    EvidenceHypothesis,
    EvidenceItem,
    EvidenceLocation,
    FailureTaxonomy,
    ToolCallRecord,
)


@pytest.fixture
def sample_evidence() -> Evidence:
    incident_id = uuid4()
    now = datetime.now(timezone.utc)
    return Evidence(
        incident_id=incident_id,
        agent="jenkins_agent",
        created_at=now,
        status="completed",
        failure_type=FailureTaxonomy.CODE_TEST_FAILURE,
        summary="Pytest failure in test_checkout with AssertionError",
        root_cause_hypotheses=[
            EvidenceHypothesis(
                hypothesis="Assertion error in checkout flow",
                failure_type=FailureTaxonomy.CODE_TEST_FAILURE,
                confidence=0.92,
                supporting_evidence=["failing_tests[0]", "error_blocks[0]"],
            ),
            EvidenceHypothesis(
                hypothesis="Secondary dependency issue",
                failure_type=FailureTaxonomy.DEPENDENCY_REGRESSION,
                confidence=0.65,
                supporting_evidence=["error_blocks[0]"],
            ),
        ],
        evidence_items=[
            EvidenceItem(
                id="build_summary",
                kind="test_result",
                source="jenkins:orders/main#42",
                timestamp=now,
                content=json.dumps({
                    "job_name": "orders/main",
                    "build_number": 42,
                    "branch": "main",
                    "commit": "abc1234",
                }),
            ),
            EvidenceItem(
                id="failing_tests[0]",
                kind="test_result",
                source="jenkins:orders/main#42",
                timestamp=now,
                content=json.dumps({
                    "name": "test_checkout_total",
                    "file": "tests/test_orders.py",
                    "line": 88,
                    "message": "AssertionError: expected $100, got $120",
                }),
                location=EvidenceLocation(file="tests/test_orders.py", line=88),
            ),
            EvidenceItem(
                id="compiler_errors[0]",
                kind="log_excerpt",
                source="jenkins:orders/main#42",
                timestamp=now,
                content="SyntaxError: invalid syntax in worker.py",
                location=EvidenceLocation(file="worker.py", line=12),
            ),
            EvidenceItem(
                id="error_blocks[0]",
                kind="log_excerpt",
                source="jenkins:orders/main#42",
                timestamp=now,
                content="Traceback (most recent call last):\n  File 'test_orders.py', line 88\nAssertionError",
            ),
            EvidenceItem(
                id="confidence_breakdown",
                kind="test_result",
                source="jenkins_agent:confidence",
                timestamp=now,
                content=json.dumps({
                    "total": 0.92,
                    "components": {
                        "rule_match": 0.90,
                        "llm_agreement": 0.93,
                        "signal_strength": 1.0,
                        "history_support": 0.85,
                    },
                    "penalties": [],
                    "hypothesis_confidences": {
                        "code_test_failure": 0.92,
                        "dependency_regression": 0.65,
                    },
                }),
            ),
        ],
        tool_calls=[ToolCallRecord(tool="get_console_text", duration_ms=120.0, ok=True)],
        confidence=0.92,
        recommended_next_steps=[
            "Inspect tests/test_orders.py:88 for calculation logic.",
            "Review recent commits touching order total pricing.",
        ],
        failed_stage="Test",
        failing_tests=["test_checkout_total"],
        error_signature="AssertionError: expected $100, got $120",
        log_truncated=False,
        redaction_applied=True,
    )


def test_generate_report_section_returns_markdown_and_structured(
    sample_evidence: Evidence,
) -> None:
    section = generate_report_section(sample_evidence)

    assert isinstance(section, JenkinsFindingsSection)
    assert "## Jenkins Findings" in section.markdown
    assert section.structured["title"] == "Jenkins Findings"
    assert "summary" in section.structured


def test_build_identity_extracted_correctly(sample_evidence: Evidence) -> None:
    section = generate_report_section(sample_evidence)
    identity = section.structured["build_identity"]

    assert identity["incident_id"] == str(sample_evidence.incident_id)
    assert identity["job_name"] == "orders/main"
    assert identity["build_number"] == 42
    assert identity["branch"] == "main"
    assert identity["commit"] == "abc1234"

    # Markdown contains build identity
    assert "orders/main" in section.markdown
    assert "#42" in section.markdown


def test_failed_stage_and_top_failures_with_location(sample_evidence: Evidence) -> None:
    section = generate_report_section(sample_evidence)

    assert section.structured["failed_stage"] == "Test"
    assert "### Failed Stage" in section.markdown
    assert "`Test`" in section.markdown

    top_failures = section.structured["top_failures"]
    assert len(top_failures) >= 2

    # Check compile error with file:line
    compile_err = next(f for f in top_failures if f["kind"] == "compile_error")
    assert compile_err["location"] == "worker.py:12"
    assert "SyntaxError" in compile_err["message"]

    # Check failing test with file:line
    test_fail = next(f for f in top_failures if f["kind"] == "test")
    assert test_fail["location"] == "tests/test_orders.py:88"
    assert test_fail["name"] == "test_checkout_total"


def test_classification_and_confidence_breakdown(sample_evidence: Evidence) -> None:
    section = generate_report_section(sample_evidence)
    classification = section.structured["classification"]

    assert classification["failure_type"] == "code_test_failure"
    assert classification["confidence"] == pytest.approx(0.92)
    assert classification["components"]["rule_match"] == pytest.approx(0.90)
    assert classification["hypothesis_confidences"]["code_test_failure"] == pytest.approx(0.92)
    assert classification["hypothesis_confidences"]["dependency_regression"] == pytest.approx(0.65)

    # Markdown includes breakdown details
    assert "### Classification & Confidence" in section.markdown
    assert "`code_test_failure`" in section.markdown
    assert "rule_match" in section.markdown


def test_key_evidence_excerpts_has_three_to_five_items(sample_evidence: Evidence) -> None:
    section = generate_report_section(sample_evidence)
    excerpts = section.structured["key_evidence_excerpts"]

    assert 3 <= len(excerpts) <= 5
    for exc in excerpts:
        assert exc["evidence_id"] in {item.id for item in sample_evidence.evidence_items}
        assert exc["line_count"] <= 10
        # Markdown contains the evidence id
        assert f"[{exc['evidence_id']}]" in section.markdown


def test_evidence_excerpts_truncated_to_max_ten_lines() -> None:
    incident_id = uuid4()
    now = datetime.now(timezone.utc)
    long_content = "\n".join(f"Traceback line {i}: error detail" for i in range(25))

    evidence = Evidence(
        incident_id=incident_id,
        agent="jenkins_agent",
        created_at=now,
        status="completed",
        failure_type=FailureTaxonomy.TIMEOUT,
        summary="Build timed out",
        evidence_items=[
            EvidenceItem(
                id="error_blocks[0]",
                kind="log_excerpt",
                source="jenkins:job#1",
                content=long_content,
            ),
            EvidenceItem(
                id="error_blocks[1]",
                kind="log_excerpt",
                source="jenkins:job#1",
                content="short block 1",
            ),
            EvidenceItem(
                id="error_blocks[2]",
                kind="log_excerpt",
                source="jenkins:job#1",
                content="short block 2",
            ),
        ],
        confidence=0.85,
    )

    section = generate_report_section(evidence)
    long_exc = next(e for e in section.structured["key_evidence_excerpts"] if e["evidence_id"] == "error_blocks[0]")
    assert long_exc["line_count"] <= 10
    assert "... [truncated" in long_exc["content"]


def test_unknown_and_missing_deduction() -> None:
    incident_id = uuid4()
    now = datetime.now(timezone.utc)

    # Truncated log with no failed stage and unknown failure type
    sparse_evidence = Evidence(
        incident_id=incident_id,
        agent="jenkins_agent",
        created_at=now,
        status="insufficient_evidence",
        failure_type=FailureTaxonomy.UNKNOWN,
        summary="Unclear failure",
        confidence=0.20,
        log_truncated=True,
        failed_stage=None,
    )

    section = generate_report_section(sparse_evidence)
    unknowns = section.structured["unknown_or_missing"]

    assert any("truncated" in u.lower() for u in unknowns)
    assert any("pipeline stage" in u.lower() for u in unknowns)
    assert any("taxonomy category" in u.lower() or "root cause" in u.lower() for u in unknowns)


def test_fallback_summary_is_two_sentences_without_llm(sample_evidence: Evidence) -> None:
    section = generate_report_section(sample_evidence, llm_client=None)
    summary = section.summary

    assert summary.count(".") >= 2
    assert "Jenkins build failed" in summary
    assert "code_test_failure" in summary


def test_llm_client_used_for_summary_when_provided(sample_evidence: Evidence) -> None:
    mock_llm_summary = "Build failed at Test stage due to a failing assertion in checkout. Developers should verify pricing logic in orders.py."

    class MockLLMClient:
        def invoke(self, messages):
            class Response:
                content = mock_llm_summary
            return Response()

    section = generate_report_section(sample_evidence, llm_client=MockLLMClient())
    assert section.summary == mock_llm_summary
    assert mock_llm_summary in section.markdown


def test_llm_client_fallback_on_error(sample_evidence: Evidence) -> None:
    class FailingLLMClient:
        def invoke(self, messages):
            raise RuntimeError("API quota exceeded")

    section = generate_report_section(sample_evidence, llm_client=FailingLLMClient())
    # Should seamlessly fall back to deterministic summary without raising
    assert "Jenkins build failed" in section.summary
    assert "code_test_failure" in section.summary


def test_recommended_next_steps_rendered(sample_evidence: Evidence) -> None:
    section = generate_report_section(sample_evidence)
    steps = section.structured["recommended_next_steps"]

    assert len(steps) == len(sample_evidence.recommended_next_steps)
    assert "Inspect tests/test_orders.py:88" in steps[0]
    assert "1. Inspect tests/test_orders.py:88" in section.markdown

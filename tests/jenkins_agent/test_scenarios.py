"""End-to-end regression coverage for labeled Jenkins failure scenarios."""

from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage

from agents.jenkins_agent.agent import (
    AgentState,
    JenkinsInvestigationAgent,
    LLMAnswer,
    _evidence_from_answer,
    _fallback_evidence,
)
from agents.jenkins_agent.classifier import rule_based_classify
from agents.jenkins_agent.parsing import parse_build
from common.models import FailureTaxonomy, IncidentCreatedEvent
from agents.jenkins_agent.tools.jenkins_client import ConsoleText


FIXTURE_ROOT = Path(__file__).parent / "fixtures"
SCENARIOS = ("tc01_unit_test", "tc02_compile_syntax", "tc02_compile_import")
TEST_STAGE = [{"name": "Test", "status": "FAILED"}]
EVENT = IncidentCreatedEvent(
    incident_id=uuid4(),
    job_name="target-app/main",
    build_number=42,
    build_url="http://jenkins/job/target-app/job/main/42/",
    branch="main",
    git_commit="scenario-fixture-commit",
    failed_stage="Test",
    timestamp=datetime.now(timezone.utc),
)


def _load_scenario(name: str) -> tuple[str, dict[str, object]]:
    directory = FIXTURE_ROOT / name
    log = (directory / "build.log").read_text(encoding="utf-8")
    ground_truth = json.loads((directory / "ground_truth.json").read_text(encoding="utf-8"))
    return log, ground_truth


def _state(log: str, *, truncated: bool = False) -> AgentState:
    parsed = parse_build(log, stage_summary=TEST_STAGE)
    evidence_items = []
    state: AgentState = {
        "incident_id": str(EVENT.incident_id),
        "event": EVENT,
        "parsed": parsed,
        "hypotheses": rule_based_classify(parsed),
        "messages": [],
        "steps_used": 0,
        "evidence": None,
        "evidence_items": evidence_items,
        "tool_calls": [],
        "tool_counts": {},
        "console_text": log,
        "log_truncated": truncated,
    }
    from agents.jenkins_agent.agent import _initial_evidence_items

    state["evidence_items"] = _initial_evidence_items(state)
    return state


def _mock_answer(ground_truth: dict[str, object]) -> LLMAnswer:
    failure_type = FailureTaxonomy(str(ground_truth["expected_failure_type"]))
    file = str(ground_truth["file"])
    line = ground_truth["line"]
    if failure_type is FailureTaxonomy.CODE_TEST_FAILURE:
        name = str(ground_truth["test_name"])
        summary = f"{name} failed in {file}:{line}; correct the implementation/test."
        recommendation = "Correct the implementation/test."
        citation = "failing_tests[0]"
    else:
        summary = f"Syntax or import error in {file}:{line}."
        recommendation = "Correct the syntax or import error."
        citation = "compiler_errors[0]"
    return LLMAnswer.model_validate(
        {
            "status": "completed",
            "failure_type": failure_type,
            "summary": summary,
            "root_cause_hypotheses": [
                {
                    "hypothesis": summary,
                    "failure_type": failure_type,
                    "confidence": 0.9,
                    "supporting_evidence": [citation],
                    "contradicting_evidence": [],
                }
            ],
            "confidence": 0.9,
            "recommended_next_steps": [recommendation],
        }
    )


def _assert_ground_truth(evidence, truth: dict[str, object]) -> None:
    assert evidence.failure_type.value == truth["expected_failure_type"]
    assert evidence.confidence >= 0.7
    assert str(truth["file"]) in evidence.summary
    assert f":{truth['line']}" in evidence.summary
    if "test_name" in truth:
        assert str(truth["test_name"]) in evidence.summary
        assert "environment problem" not in evidence.summary.casefold()
        assert any(
            "correct the implementation/test" in recommendation.casefold()
            for recommendation in evidence.recommended_next_steps
        )


class MockLLM:
    def __init__(self, response: AIMessage | Exception) -> None:
        self.response = response
        self.calls = 0

    def bind_tools(self, _tools):
        return self

    async def ainvoke(self, _messages):
        self.calls += 1
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class ScenarioJenkinsClient:
    def __init__(self, log: str, *, truncated: bool = False) -> None:
        self.log = log
        self.truncated = truncated

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get_build(self, _job, number):
        return {"number": number, "result": "FAILURE", "duration": 100}

    async def get_stage_summary(self, _job, _number):
        return {"stages": [{"name": "Test", "status": "FAILED", "durationMillis": 50}]}

    async def get_console_text(self, _job, _number):
        return ConsoleText(self.log, self.truncated, False)

    async def get_test_report(self, _job, _number):
        return {"failCount": 1, "totalCount": 1}

    async def get_test_report_xml(self, _job, _number):
        return None

    async def get_build_history(self, _job):
        return []

    async def get_previous_successful_build(self, _job):
        return {}


class ScenarioEvidenceStore:
    def __init__(self) -> None:
        self.written = []

    async def write(self, evidence):
        self.written.append(evidence)


@pytest.mark.parametrize("scenario", SCENARIOS)
@pytest.mark.asyncio
async def test_scenario_outputs_match_ground_truth_with_mock_llm_and_rules(scenario: str) -> None:
    log, truth = _load_scenario(scenario)
    answer = _mock_answer(truth)
    llm = MockLLM(AIMessage(content=answer.model_dump_json()))
    llm_store = ScenarioEvidenceStore()
    jenkins_factory = lambda: ScenarioJenkinsClient(log)
    llm_agent = JenkinsInvestigationAgent(
        llm,
        llm_store,
        jenkins_client_factory=jenkins_factory,
        per_call_timeout=2,
        total_timeout=10,
        llm_timeout=2,
    )
    llm_evidence = await llm_agent.run_event(EVENT)
    rules_store = ScenarioEvidenceStore()
    rules_agent = JenkinsInvestigationAgent(
        MockLLM(RuntimeError("force deterministic rules fallback")),
        rules_store,
        jenkins_client_factory=jenkins_factory,
        per_call_timeout=2,
        total_timeout=10,
        llm_timeout=2,
    )
    rules_evidence = await rules_agent.run_event(EVENT)

    _assert_ground_truth(llm_evidence, truth)
    _assert_ground_truth(rules_evidence, truth)
    assert len(llm_store.written) == 1
    assert len(rules_store.written) == 1
    assert llm.calls == 1


def test_test_stage_pytest_collection_syntax_error_is_compilation_failure() -> None:
    log, truth = _load_scenario("tc02_compile_syntax")

    parsed = parse_build(log, stage_summary=TEST_STAGE)
    hypotheses = rule_based_classify(parsed)

    assert parsed.failed_stage == "Test"
    assert hypotheses[0].failure_type is FailureTaxonomy.BUILD_COMPILATION_FAILURE
    assert parsed.compiler_errors[0].file.endswith("target_app/app/main.py")
    assert parsed.compiler_errors[0].line == truth["line"]


def test_collection_import_error_is_compilation_failure() -> None:
    log, truth = _load_scenario("tc02_compile_import")

    parsed = parse_build(log, stage_summary=TEST_STAGE)
    hypotheses = rule_based_classify(parsed)

    assert hypotheses[0].failure_type is FailureTaxonomy.BUILD_COMPILATION_FAILURE
    assert parsed.compiler_errors[0].file.endswith("target_app/app/main.py")
    assert parsed.compiler_errors[0].line == truth["line"]


def test_llm_cannot_relabel_confirmed_test_failure_as_environment_problem() -> None:
    log, truth = _load_scenario("tc01_unit_test")
    state = _state(log)
    unsupported_answer = LLMAnswer.model_validate(
        {
            "failure_type": "infra_network_failure",
            "summary": "This is probably an environment problem.",
            "root_cause_hypotheses": [
                {
                    "hypothesis": "The environment is unavailable.",
                    "failure_type": "infra_network_failure",
                    "confidence": 0.9,
                }
            ],
            "confidence": 0.9,
        }
    )

    evidence = _evidence_from_answer(state, unsupported_answer)

    _assert_ground_truth(evidence, truth)
    assert evidence.root_cause_hypotheses[0].failure_type is FailureTaxonomy.CODE_TEST_FAILURE


def test_simultaneous_test_failure_and_lint_warning_remains_test_failure() -> None:
    log, truth = _load_scenario("tc01_unit_test")
    combined_log = log + "\nWARNING flake8: style warning in target_app/app/main.py:8\n"
    state = _state(combined_log)

    evidence = _fallback_evidence(state)

    _assert_ground_truth(evidence, truth)
    assert evidence.failure_type is FailureTaxonomy.CODE_TEST_FAILURE


def test_truncated_log_lowers_confidence() -> None:
    log, truth = _load_scenario("tc01_unit_test")
    complete_state = _state(log)
    truncated_state = _state(log, truncated=True)
    complete = _fallback_evidence(complete_state)
    truncated = _fallback_evidence(truncated_state)
    llm_truncated = _evidence_from_answer(truncated_state, _mock_answer(truth))

    assert truncated.confidence < complete.confidence
    assert llm_truncated.confidence < complete.confidence

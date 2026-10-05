from datetime import datetime, timezone
import json
from uuid import uuid4

from langchain_core.messages import AIMessage
import pytest

from agents.jenkins_agent.agent import (
    MAX_CALLS_PER_TOOL,
    MAX_LOG_LINES_PER_CALL,
    MAX_STEPS,
    JenkinsInvestigationAgent,
    JenkinsInvestigationTools,
    _process_message,
)
from agents.jenkins_agent.tools.jenkins_client import ConsoleText
from common.models import (
    Evidence,
    EvidenceHypothesis,
    FailureTaxonomy,
    IncidentCreatedEvent,
)


EVENT = IncidentCreatedEvent(
    incident_id=uuid4(),
    job_name="orders/main",
    build_number=7,
    build_url="http://jenkins/job/orders/job/main/7/",
    branch="main",
    git_commit="commit-7",
    failed_stage="Tests",
    timestamp=datetime.now(timezone.utc),
)

JUNIT_XML = """<testsuite><testcase name="test_total" file="tests/test_orders.py" line="14">
<failure message="AssertionError: expected 3">assert 2 == 3</failure></testcase></testsuite>"""


def final_answer() -> str:
    return json.dumps(
        {
            "status": "completed",
            "failure_type": "code_test_failure",
            "summary": "The test assertion failed; see failing_tests[0].",
            "root_cause_hypotheses": [
                {
                    "hypothesis": "The test assertion does not match the actual result.",
                    "failure_type": "code_test_failure",
                    "confidence": 0.9,
                    "supporting_evidence": ["failing_tests[0]"],
                    "contradicting_evidence": [],
                }
            ],
            "confidence": 0.9,
            "recommended_next_steps": ["Review the assertion and implementation."],
        }
    )


class FakeJenkinsClient:
    async def __aenter__(self) -> "FakeJenkinsClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def get_build(self, job: str, number: int) -> dict[str, object]:
        return {"number": number, "result": "FAILURE", "duration": 1200}

    async def get_stage_summary(self, job: str, number: int) -> dict[str, object]:
        return {"stages": [{"name": "Tests", "status": "FAILED", "durationMillis": 800}]}

    async def get_console_text(self, job: str, number: int) -> ConsoleText:
        return ConsoleText(
            "FAILED tests/test_orders.py::test_total - AssertionError: expected 3",
            False,
            False,
        )

    async def get_test_report(self, job: str, number: int) -> dict[str, int]:
        return {"failCount": 1, "totalCount": 1}

    async def get_test_report_xml(self, job: str, number: int) -> str:
        return JUNIT_XML

    async def get_build_history(self, job: str) -> list[dict[str, object]]:
        return [{"number": 7, "result": "FAILURE", "timestamp": 100}]

    async def get_previous_successful_build(self, job: str) -> dict[str, object]:
        return {
            "number": 6,
            "result": "SUCCESS",
            "actions": [{"lastBuiltRevision": {"SHA1": "previous-commit"}}],
        }


class FakeEvidenceStore:
    def __init__(self) -> None:
        self.written: list[Evidence] = []

    async def write(self, evidence: Evidence) -> None:
        self.written.append(evidence)


class FakeLLM:
    def __init__(self, responses: list[AIMessage | Exception]) -> None:
        self.responses = responses
        self.calls = 0
        self.bound_tool_names: set[str] = set()
        self.inputs: list[list[object]] = []

    def bind_tools(self, tools: list[object]) -> "FakeLLM":
        self.bound_tool_names = {tool.name for tool in tools}
        return self

    async def ainvoke(self, messages: list[object]) -> AIMessage:
        self.calls += 1
        self.inputs.append(messages)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class RateLimitError(Exception):
    status_code = 429


def make_agent(
    responses: list[AIMessage | Exception],
    *,
    sleep=None,
    jenkins_client_factory=FakeJenkinsClient,
) -> tuple[JenkinsInvestigationAgent, FakeEvidenceStore, FakeLLM]:
    store = FakeEvidenceStore()
    llm = FakeLLM(responses)
    agent = JenkinsInvestigationAgent(
        llm,
        store,
        jenkins_client_factory=jenkins_client_factory,
        sleep=sleep or _no_sleep,
        per_call_timeout=2,
        llm_timeout=2,
        total_timeout=10,
    )
    return agent, store, llm


async def _no_sleep(_: float) -> None:
    return None


@pytest.mark.asyncio
async def test_fetch_context_parses_build_and_exposes_all_read_only_tools() -> None:
    agent, _, llm = make_agent([AIMessage(content=final_answer())])

    evidence = await agent.run_event(EVENT)

    assert evidence.failure_type is FailureTaxonomy.CODE_TEST_FAILURE
    assert "failing_tests[0]" in {item.id for item in evidence.evidence_items}
    assert "last_success" in {item.id for item in evidence.evidence_items}
    assert all(call.ok for call in evidence.tool_calls[:7])
    assert llm.bound_tool_names == {
        "get_build_summary",
        "get_stage_summary",
        "get_error_blocks",
        "get_log_range",
        "get_test_report",
        "get_build_history",
        "compare_with_last_success",
    }
    assert evidence.redaction_applied is True


@pytest.mark.asyncio
async def test_get_log_range_is_inclusive_and_bounded_to_300_lines() -> None:
    provider = JenkinsInvestigationTools(
        {"console_text": "\n".join(f"line {index}" for index in range(1, 402))}
    )
    excerpt = json.loads(await provider.get_log_range(2, MAX_LOG_LINES_PER_CALL + 1))

    assert len(excerpt["lines"]) == MAX_LOG_LINES_PER_CALL
    assert excerpt["lines"][0] == "line 2"
    with pytest.raises(ValueError, match="at most 300 lines"):
        await provider.get_log_range(1, MAX_LOG_LINES_PER_CALL + 1)
    with pytest.raises(ValueError, match="end_line"):
        await provider.get_log_range(4, 3)


@pytest.mark.asyncio
async def test_invalid_json_retries_once_then_accepts_valid_answer() -> None:
    agent, store, llm = make_agent([AIMessage(content="not-json"), AIMessage(content=final_answer())])

    evidence = await agent.run_event(EVENT)

    assert llm.calls == 2
    assert len(store.written) == 1
    assert evidence.summary.startswith("The test assertion failed")


@pytest.mark.asyncio
async def test_invalid_evidence_citation_retries_once() -> None:
    invalid_answer = json.loads(final_answer())
    invalid_answer["root_cause_hypotheses"][0]["supporting_evidence"] = ["made_up_id"]
    agent, _, llm = make_agent(
        [AIMessage(content=json.dumps(invalid_answer)), AIMessage(content=final_answer())]
    )

    evidence = await agent.run_event(EVENT)

    assert llm.calls == 2
    assert evidence.root_cause_hypotheses[0].supporting_evidence == ["failing_tests[0]"]


@pytest.mark.asyncio
async def test_second_invalid_answer_falls_back_to_rules() -> None:
    agent, store, llm = make_agent([AIMessage(content="bad"), AIMessage(content="still bad")])

    evidence = await agent.run_event(EVENT)

    assert llm.calls == 2
    assert len(store.written) == 1
    assert evidence.failure_type is FailureTaxonomy.CODE_TEST_FAILURE
    assert evidence.root_cause_hypotheses[0].supporting_evidence == ["failing_tests[0]"]


@pytest.mark.asyncio
async def test_same_commit_previous_passing_tests_add_flaky_candidate() -> None:
    class SameCommitJenkinsClient(FakeJenkinsClient):
        async def get_test_report(self, job: str, number: int) -> dict[str, int]:
            if number == 6:
                return {"passCount": 4, "failCount": 0, "totalCount": 4}
            return await super().get_test_report(job, number)

        async def get_previous_successful_build(self, job: str) -> dict[str, object]:
            return {
                "number": 6,
                "result": "SUCCESS",
                "actions": [{"lastBuiltRevision": {"SHA1": "commit-7"}}],
            }

    agent, _, _ = make_agent(
        [AIMessage(content="invalid"), AIMessage(content="still invalid")],
        jenkins_client_factory=SameCommitJenkinsClient,
    )

    evidence = await agent.run_event(EVENT)

    assert evidence.failure_type is FailureTaxonomy.CODE_TEST_FAILURE
    flaky = next(
        hypothesis
        for hypothesis in evidence.root_cause_hypotheses
        if hypothesis.failure_type is FailureTaxonomy.FLAKY_TEST
    )
    assert flaky.confidence == 0.6
    assert flaky.supporting_evidence


@pytest.mark.asyncio
async def test_repeated_rate_limit_falls_back_without_unbounded_retry() -> None:
    agent, store, llm = make_agent(
        [RateLimitError("429") for _ in range(3)],
        sleep=_no_sleep,
    )

    evidence = await agent.run_event(EVENT)

    assert llm.calls == 3
    assert len(store.written) == 1
    assert evidence.failure_type is FailureTaxonomy.CODE_TEST_FAILURE


@pytest.mark.asyncio
async def test_console_secrets_are_redacted_before_model_and_evidence() -> None:
    class SecretJenkinsClient(FakeJenkinsClient):
        async def get_console_text(self, job: str, number: int) -> ConsoleText:
            return ConsoleText("ERROR password=do-not-leak", False, False)

    agent, store, llm = make_agent(
        [AIMessage(content="invalid"), AIMessage(content="still invalid")],
        jenkins_client_factory=SecretJenkinsClient,
    )

    evidence = await agent.run_event(EVENT)
    serialized_evidence = evidence.model_dump_json()
    model_input = str(llm.inputs)

    assert len(store.written) == 1
    assert "do-not-leak" not in serialized_evidence
    assert "do-not-leak" not in model_input
    assert "[REDACTED]" in serialized_evidence


@pytest.mark.asyncio
async def test_tool_exception_is_recorded_and_reasoning_continues() -> None:
    call = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "get_log_range",
                "args": {"start_line": 1, "end_line": MAX_LOG_LINES_PER_CALL + 1},
                "id": "bad-range",
                "type": "tool_call",
            }
        ],
    )
    agent, store, llm = make_agent([call, AIMessage(content=final_answer())])

    evidence = await agent.run_event(EVENT)

    failed_calls = [call for call in evidence.tool_calls if call.tool == "get_log_range"]
    assert len(failed_calls) == 1
    assert failed_calls[0].ok is False
    assert llm.calls == 2
    assert len(store.written) == 1


@pytest.mark.asyncio
async def test_max_steps_and_tool_call_limit_bound_repeated_tool_loop() -> None:
    responses = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "get_build_summary",
                    "args": {},
                    "id": f"call-{index}",
                    "type": "tool_call",
                }
            ],
        )
        for index in range(MAX_STEPS)
    ]
    agent, store, llm = make_agent(responses)

    evidence = await agent.run_event(EVENT)

    calls = [call for call in evidence.tool_calls if call.tool == "get_build_summary"]
    assert llm.calls == MAX_STEPS
    assert len(calls) == MAX_STEPS
    assert sum(call.ok for call in calls) == MAX_CALLS_PER_TOOL
    assert evidence.failure_type is FailureTaxonomy.CODE_TEST_FAILURE
    assert len(store.written) == 1


@pytest.mark.asyncio
async def test_consumer_acks_only_after_evidence_is_returned() -> None:
    evidence = Evidence(
        incident_id=EVENT.incident_id,
        created_at=datetime.now(timezone.utc),
        status="insufficient_evidence",
        failure_type=FailureTaxonomy.UNKNOWN,
        summary="Not enough evidence.",
        root_cause_hypotheses=[
            EvidenceHypothesis(
                hypothesis="Unknown",
                failure_type=FailureTaxonomy.UNKNOWN,
                confidence=0.2,
            )
        ],
        confidence=0.2,
        redaction_applied=True,
    )

    class PersistingAgent:
        persisted = False

        async def run_event(self, event: IncidentCreatedEvent) -> Evidence:
            self.persisted = True
            return evidence

    class Message:
        body = EVENT.model_dump_json().encode()
        acked = False
        nacked = False

        async def ack(self) -> None:
            assert agent.persisted
            self.acked = True

        async def nack(self, *, requeue: bool) -> None:
            self.nacked = True

    agent = PersistingAgent()
    message = Message()
    await _process_message(message, agent)  # type: ignore[arg-type]

    assert message.acked is True
    assert message.nacked is False


@pytest.mark.asyncio
async def test_consumer_dead_letters_on_processing_failure() -> None:
    class FailedAgent:
        async def run_event(self, event: IncidentCreatedEvent) -> Evidence:
            raise RuntimeError("temporary failure")

    class Message:
        body = EVENT.model_dump_json().encode()
        requeue: bool | None = None

        async def ack(self) -> None:
            raise AssertionError("failed messages must not be acked")

        async def nack(self, *, requeue: bool) -> None:
            self.requeue = requeue

    message = Message()
    await _process_message(message, FailedAgent())  # type: ignore[arg-type]

    assert message.requeue is True
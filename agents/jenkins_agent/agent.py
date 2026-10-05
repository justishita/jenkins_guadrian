"""LangGraph Jenkins build investigator and RabbitMQ worker."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from collections import Counter
from typing import Any, Protocol, TypedDict
from uuid import UUID

import aio_pika
from aio_pika.abc import AbstractIncomingMessage
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.graph import END, StateGraph
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from agents.jenkins_agent.classifier import Hypothesis, rule_based_classify
from agents.jenkins_agent.config import JenkinsAgentSettings, load_settings
from agents.jenkins_agent.parsing import ParsedBuild, parse_build
from agents.jenkins_agent.tools.jenkins_client import JenkinsClient
from common.evidence_store import FileEvidenceStore
from common.llm_client import LLMClient
from common.models import (
    Evidence,
    EvidenceHypothesis,
    EvidenceItem,
    FailureTaxonomy,
    IncidentCreatedEvent,
    ToolCallRecord,
)
from common.redaction import redact


logger = logging.getLogger(__name__)
MAX_STEPS = 8
MAX_CALLS_PER_TOOL = 3
MAX_LOG_LINES_PER_CALL = 300
MAX_TEST_FAILURES_IN_PROMPT = 5
MAX_TOOL_OUTPUT_CHARS = 12_000
MAX_RATE_LIMIT_RETRIES = 2
SYSTEM_PROMPT = """You are a CI failure analyst investigating one Jenkins incident.
Ground every claim in a cited evidence ID. If evidence is insufficient, say so
with low confidence. Never invent file names or line numbers. Never suggest
disabling tests or security checks. Use only the supplied read-only tools.
For assertion failures, name the failing test and source file/line when known,
classify as code_test_failure, and recommend "correct the implementation/test".
For SyntaxError, ImportError, or collection failures, name the source file/line
when known and classify as build_compilation_failure, including collection
errors surfaced in the Test stage. Do not call a confirmed code/test failure an
environment problem.
Respond with one JSON object containing status, failure_type, summary,
root_cause_hypotheses (hypothesis, failure_type, confidence,
supporting_evidence, contradicting_evidence), confidence, and
recommended_next_steps. Cite only evidence IDs shown in the context or tool
observations. failure_type must use the provided shared taxonomy exactly."""


class EvidenceWriter(Protocol):
    async def write(self, evidence: Evidence) -> None: ...


class EmptyToolInput(BaseModel):
    """Input schema for tools that need no arguments."""

    model_config = ConfigDict(extra="forbid")


class LogRangeInput(BaseModel):
    """Validated inclusive line range for a Jenkins console excerpt."""

    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)

    @classmethod
    def validate_range(cls, start_line: int, end_line: int) -> None:
        if end_line < start_line:
            raise ValueError("end_line must be greater than or equal to start_line")
        if end_line - start_line + 1 > MAX_LOG_LINES_PER_CALL:
            raise ValueError(f"a log range may contain at most {MAX_LOG_LINES_PER_CALL} lines")


class LLMAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str = "completed"
    failure_type: FailureTaxonomy
    summary: str = Field(min_length=1)
    root_cause_hypotheses: list[EvidenceHypothesis]
    confidence: float = Field(ge=0, le=1)
    recommended_next_steps: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def unknown_requires_low_confidence(self) -> "LLMAnswer":
        if self.failure_type is FailureTaxonomy.UNKNOWN and self.confidence > 0.4:
            raise ValueError("unknown classifications must have low confidence (0.4 or less)")
        return self


class AgentState(TypedDict, total=False):
    incident_id: str
    event: IncidentCreatedEvent
    parsed: ParsedBuild
    hypotheses: list[Hypothesis]
    messages: list[BaseMessage]
    steps_used: int
    evidence: Evidence | None
    evidence_items: list[EvidenceItem]
    tool_calls: list[ToolCallRecord]
    tool_counts: dict[str, int]
    build_summary: dict[str, Any]
    stage_summary: dict[str, Any]
    console_text: str
    log_truncated: bool
    test_report: dict[str, Any] | None
    build_history: list[dict[str, Any]]
    last_success: dict[str, Any] | None
    last_success_test_report: dict[str, Any] | None
    validation_retries: int
    continue_reasoning: bool
    llm_failed: bool


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, Mapping):
        return {str(key): _redact_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(item) for item in value]
    return value


def _json_text(value: Any, *, limit: int = MAX_TOOL_OUTPUT_CHARS) -> str:
    safe = _redact_value(value)
    text = json.dumps(safe, default=str, ensure_ascii=True)
    if len(text) > limit:
        text = text[:limit] + "...[truncated]"
    return text


def _message_content(message: BaseMessage) -> str:
    content = message.content
    return content if isinstance(content, str) else json.dumps(content, default=str)


def _failing_test_summary(parsed: ParsedBuild) -> dict[str, Any]:
    failures = parsed.failing_tests
    file_counts = Counter(test.file or "unknown" for test in failures)
    return {
        "total_count": len(failures),
        "count_by_file": dict(sorted(file_counts.items())),
        "top_failures": [
            {
                "name": test.name,
                "file": test.file,
                "line": test.line,
                "message": test.message[:500],
            }
            for test in failures[:MAX_TEST_FAILURES_IN_PROMPT]
        ],
    }


def _is_checkout_or_post_failure(stage: str | None) -> bool:
    normalized = (stage or "").strip().casefold()
    return normalized == "checkout" or "post" in normalized


def _failed_stage_from_summary(stage_summary: Any) -> str | None:
    raw_stages = (
        stage_summary.get("stages", [])
        if isinstance(stage_summary, Mapping)
        else stage_summary
    )
    if not isinstance(raw_stages, Sequence) or isinstance(raw_stages, (str, bytes)):
        return None
    for stage in reversed(raw_stages):
        if not isinstance(stage, Mapping):
            continue
        result = str(stage.get("status", stage.get("result", ""))).upper()
        if result in {"FAILURE", "FAILED"}:
            name = stage.get("name", stage.get("stage"))
            return str(name) if name else None
    return None


def _test_failure_count_summary(parsed: ParsedBuild) -> str:
    failures = parsed.failing_tests
    if not failures:
        return ""
    counts = Counter(test.file or "unknown file" for test in failures)
    file_counts = ", ".join(f"{path}: {count}" for path, count in sorted(counts.items()))
    examples = []
    for test in failures[:MAX_TEST_FAILURES_IN_PROMPT]:
        location = _format_location(test.file, test.line)
        examples.append(f"{test.name}" + (f" ({location})" if location else ""))
    remaining = len(failures) - len(examples)
    top_five = ", ".join(examples)
    if remaining:
        top_five += f", and {remaining} more"
    return (
        f"{len(failures)} failing tests across {len(counts)} files "
        f"({file_counts}). Top failures: {top_five}."
    )


def _llm_error_blocks(parsed: ParsedBuild) -> list[str]:
    """Avoid sending all individual pytest tracebacks when a failure summary suffices."""
    if not parsed.failing_tests:
        return parsed.error_blocks
    return [
        block
        for block in parsed.error_blocks
        if not re.search(
            r"^\s*(?:_{3,}\s*.+\s*_{3,}|FAILED\s+.+?\.py::)",
            block,
            re.MULTILINE,
        )
    ]


def _append_test_failure_summary(summary: str, parsed: ParsedBuild) -> str:
    count_summary = _test_failure_count_summary(parsed)
    return _append_fact(summary, count_summary) if count_summary else summary


def _safe_messages(messages: Sequence[BaseMessage]) -> list[BaseMessage]:
    safe_messages: list[BaseMessage] = []
    for message in messages:
        content = _redact_value(message.content)
        if isinstance(message, AIMessage):
            tool_calls = [
                {
                    "name": call["name"],
                    "args": _redact_value(call.get("args", {})),
                    "id": call.get("id", ""),
                    "type": "tool_call",
                }
                for call in message.tool_calls
            ]
            safe_messages.append(AIMessage(content=content, tool_calls=tool_calls))
        elif isinstance(message, ToolMessage):
            safe_messages.append(
                ToolMessage(content=content, tool_call_id=message.tool_call_id, name=message.name)
            )
        elif isinstance(message, SystemMessage):
            safe_messages.append(SystemMessage(content=content))
        else:
            safe_messages.append(HumanMessage(content=content))
    return safe_messages


def _content_as_text(response: AIMessage) -> str:
    content = response.content
    if isinstance(content, str):
        text = content
    else:
        text = "".join(
            block.get("text", "") for block in content if isinstance(block, Mapping)
        )
    text = redact(text).strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    return text


def _is_rate_limited(error: BaseException) -> bool:
    response = getattr(error, "response", None)
    status = getattr(error, "status_code", None) or getattr(response, "status_code", None)
    return status == 429 or "429" in str(error)


def _build_commit(build: Mapping[str, Any] | None) -> str | None:
    if not build:
        return None
    for action in build.get("actions", []):
        revision = action.get("lastBuiltRevision", {}) if isinstance(action, Mapping) else {}
        sha = revision.get("SHA1") if isinstance(revision, Mapping) else None
        if sha:
            return str(sha)
    return None


def _selected_build_fields(build: Mapping[str, Any] | None) -> dict[str, Any]:
    if not build:
        return {}
    keys = ("number", "result", "building", "timestamp", "duration", "displayName")
    return {key: build[key] for key in keys if key in build}


def _tests_passed(report: Mapping[str, Any] | None) -> bool | None:
    if not report:
        return None
    try:
        total = int(report.get("totalCount", 0))
        failures = int(report.get("failCount", 0)) + int(report.get("skipCount", 0))
    except (TypeError, ValueError):
        return None
    return total > 0 and failures == 0


def _error_text(value: BaseException) -> str:
    return redact(f"{type(value).__name__}: {value}")


class JenkinsInvestigationTools:
    """Read-only, Pydantic-typed tools exposing the fetched Jenkins context."""

    def __init__(self, state: AgentState) -> None:
        self.state = state

    async def get_build_summary(self) -> str:
        """Return the current Jenkins build result, number, duration, and status."""
        return _json_text(
            {"evidence_id": "build_summary", "summary": self.state.get("build_summary", {})}
        )

    async def get_stage_summary(self) -> str:
        """Return the normalized Jenkins pipeline stage names and results."""
        parsed = self.state.get("parsed")
        return _json_text(
            [
                {
                    "evidence_id": f"stages[{index}]",
                    "name": stage.name,
                    "result": stage.result,
                    "duration_ms": stage.duration_ms,
                }
                for index, stage in enumerate(parsed.stages if parsed else [])
            ]
        )

    async def get_error_blocks(self) -> str:
        """Return extracted console error blocks with their stable evidence IDs."""
        parsed = self.state.get("parsed")
        if parsed is None:
            blocks: list[str] = []
            failures: dict[str, Any] = {
                "total_count": 0,
                "count_by_file": {},
                "top_failures": [],
            }
        else:
            blocks = _llm_error_blocks(parsed)
            failures = _failing_test_summary(parsed)
        return _json_text(
            {
                "failing_test_summary": failures,
                "error_blocks": [
                {"evidence_id": f"error_blocks[{index}]", "content": block}
                    for index, block in enumerate(blocks)
                ],
            }
        )

    async def get_log_range(self, start_line: int, end_line: int) -> str:
        """Return an inclusive console line range, capped at 300 lines per call."""
        LogRangeInput.validate_range(start_line, end_line)
        lines = self.state.get("console_text", "").splitlines()
        excerpt = lines[start_line - 1 : end_line]
        return _json_text(
            {
                "start_line": start_line,
                "end_line": min(end_line, len(lines)),
                "lines": excerpt,
            }
        )

    async def get_test_report(self) -> str:
        """Return the Jenkins test report summary, when one is available."""
        report = self.state.get("test_report")
        return _json_text({"evidence_id": "test_report" if report is not None else None, "report": report})

    async def get_build_history(self) -> str:
        """Return up to 30 recent Jenkins build numbers, results, and timestamps."""
        history = self.state.get("build_history", [])
        safe_history = [
            {key: item[key] for key in ("number", "result", "timestamp", "duration") if key in item}
            for item in history[:30]
        ]
        return _json_text(
            {"evidence_id": "build_history" if safe_history else None, "builds": safe_history}
        )

    async def compare_with_last_success(self) -> str:
        """Compare this incident commit with the most recent successful Jenkins build."""
        event = self.state["event"]
        previous = self.state.get("last_success")
        previous_commit = _build_commit(previous)
        return _json_text(
            {
                "incident_commit": event.git_commit,
                "last_successful_build": _selected_build_fields(previous),
                "last_success_commit": previous_commit,
                "same_commit": previous_commit == event.git_commit if previous_commit else None,
                "tests_passed": _tests_passed(self.state.get("last_success_test_report")),
                "evidence_id": "last_success" if previous else None,
                "test_report_evidence_id": (
                    "last_success_test_report"
                    if self.state.get("last_success_test_report")
                    else None
                ),
                "tests_passed": _tests_passed(self.state.get("last_success_test_report")),
                "evidence_id": "last_success",
                "test_report_evidence_id": "last_success_test_report",
            }
        )


def _tools_for_state(state: AgentState) -> list[StructuredTool]:
    """Build the LLM-visible tool set from methods with explicit input schemas."""
    provider = JenkinsInvestigationTools(state)
    return [
        StructuredTool.from_function(
            coroutine=provider.get_build_summary,
            name="get_build_summary",
            description=provider.get_build_summary.__doc__ or "Get build summary",
            args_schema=EmptyToolInput,
        ),
        StructuredTool.from_function(
            coroutine=provider.get_stage_summary,
            name="get_stage_summary",
            description=provider.get_stage_summary.__doc__ or "Get stage summary",
            args_schema=EmptyToolInput,
        ),
        StructuredTool.from_function(
            coroutine=provider.get_error_blocks,
            name="get_error_blocks",
            description=provider.get_error_blocks.__doc__ or "Get error blocks",
            args_schema=EmptyToolInput,
        ),
        StructuredTool.from_function(
            coroutine=provider.get_log_range,
            name="get_log_range",
            description=provider.get_log_range.__doc__ or "Get a bounded console range",
            args_schema=LogRangeInput,
        ),
        StructuredTool.from_function(
            coroutine=provider.get_test_report,
            name="get_test_report",
            description=provider.get_test_report.__doc__ or "Get test report",
            args_schema=EmptyToolInput,
        ),
        StructuredTool.from_function(
            coroutine=provider.get_build_history,
            name="get_build_history",
            description=provider.get_build_history.__doc__ or "Get build history",
            args_schema=EmptyToolInput,
        ),
        StructuredTool.from_function(
            coroutine=provider.compare_with_last_success,
            name="compare_with_last_success",
            description=provider.compare_with_last_success.__doc__ or "Compare with last success",
            args_schema=EmptyToolInput,
        ),
    ]


class JenkinsInvestigationAgent:
    """Fetch context, reason with bounded read-only tools, and persist Evidence."""

    def __init__(
        self,
        llm_client: LLMClient,
        evidence_store: EvidenceWriter,
        *,
        jenkins_client_factory: Callable[[], JenkinsClient] = JenkinsClient,
        per_call_timeout: float = 20.0,
        total_timeout: float = 180.0,
        llm_timeout: float = 30.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.llm_client = llm_client
        self.evidence_store = evidence_store
        self.jenkins_client_factory = jenkins_client_factory
        self.per_call_timeout = per_call_timeout
        self.total_timeout = total_timeout
        self.llm_timeout = llm_timeout
        self.sleep = sleep
        graph = StateGraph(AgentState)
        graph.add_node("fetch_context", self.fetch_context)
        graph.add_node("reason", self.reason)
        graph.add_node("finalize", self.finalize)
        graph.set_entry_point("fetch_context")
        graph.add_edge("fetch_context", "reason")
        graph.add_conditional_edges(
            "reason",
            self._next_node,
            {"reason": "reason", "finalize": "finalize"},
        )
        graph.add_edge("finalize", END)
        self.graph = graph.compile()

    async def _timed_jenkins_call(
        self,
        name: str,
        operation: Awaitable[Any],
        args: dict[str, Any],
        records: list[ToolCallRecord],
    ) -> Any:
        started = time.perf_counter()
        try:
            result = await asyncio.wait_for(operation, timeout=self.per_call_timeout)
        except Exception as error:
            records.append(
                ToolCallRecord(
                    tool=name,
                    args=_redact_value(args),
                    duration_ms=(time.perf_counter() - started) * 1000,
                    ok=False,
                )
            )
            logger.warning(
                "Jenkins context request failed",
                extra={"tool": name, "error": _error_text(error)},
            )
            return None
        records.append(
            ToolCallRecord(
                tool=name,
                args=_redact_value(args),
                duration_ms=(time.perf_counter() - started) * 1000,
                ok=True,
            )
        )
        return result

    async def fetch_context(self, state: AgentState) -> dict[str, Any]:
        """Fetch Jenkins context, parse it, and seed deterministic hypotheses."""
        event = state["event"]
        calls = state.setdefault("tool_calls", [])
        client = self.jenkins_client_factory()
        async with client:
            build, stages, console, test_report, junit_xml, history, last_success = await asyncio.gather(
                self._timed_jenkins_call(
                    "jenkins.get_build", client.get_build(event.job_name, event.build_number),
                    {"job_name": event.job_name, "build_number": event.build_number}, calls,
                ),
                self._timed_jenkins_call(
                    "jenkins.get_stage_summary", client.get_stage_summary(event.job_name, event.build_number),
                    {"job_name": event.job_name, "build_number": event.build_number}, calls,
                ),
                self._timed_jenkins_call(
                    "jenkins.get_console_text", client.get_console_text(event.job_name, event.build_number),
                    {"job_name": event.job_name, "build_number": event.build_number}, calls,
                ),
                self._timed_jenkins_call(
                    "jenkins.get_test_report", client.get_test_report(event.job_name, event.build_number),
                    {"job_name": event.job_name, "build_number": event.build_number}, calls,
                ),
                self._timed_jenkins_call(
                    "jenkins.get_test_report_xml", client.get_test_report_xml(event.job_name, event.build_number),
                    {"job_name": event.job_name, "build_number": event.build_number}, calls,
                ),
                self._timed_jenkins_call(
                    "jenkins.get_build_history", client.get_build_history(event.job_name),
                    {"job_name": event.job_name, "limit": 30}, calls,
                ),
                self._timed_jenkins_call(
                    "jenkins.get_previous_successful_build", client.get_previous_successful_build(event.job_name),
                    {"job_name": event.job_name}, calls,
                ),
            )
            last_success_test_report = None
            previous_number = last_success.get("number") if isinstance(last_success, dict) else None
            if isinstance(previous_number, int):
                last_success_test_report = await self._timed_jenkins_call(
                    "jenkins.get_last_success_test_report",
                    client.get_test_report(event.job_name, previous_number),
                    {"job_name": event.job_name, "build_number": previous_number},
                    calls,
                )

        console_text = console.text if console is not None else ""
        failed_stage = event.failed_stage or _failed_stage_from_summary(stages)
        console_only_failure = _is_checkout_or_post_failure(failed_stage)
        if console_only_failure:
            test_report = None
            junit_xml = None
        parsed = parse_build(
            console_text,
            stage_summary=stages,
            failed_stage=failed_stage,
            junit_xml=junit_xml,
        )
        previous_commit = _build_commit(last_success if isinstance(last_success, dict) else None)
        previous_tests_passed = _tests_passed(
            last_success_test_report if isinstance(last_success_test_report, dict) else None
        )
        if (
            parsed.failing_tests
            and previous_tests_passed is True
            and previous_commit
            and previous_commit.casefold() == event.git_commit.casefold()
        ):
            parsed.error_blocks.append(
                f"Previous build {previous_number} passed its test report on the same commit."
            )
        hypotheses = rule_based_classify(parsed)
        build_summary = _selected_build_fields(build)
        if "number" not in build_summary:
            build_summary["number"] = event.build_number
        if "result" not in build_summary:
            build_summary["result"] = "FAILURE"

        state.update(
            parsed=parsed,
            hypotheses=hypotheses,
            build_summary=build_summary,
            stage_summary=stages if isinstance(stages, dict) else {},
            console_text=console_text,
            log_truncated=bool(getattr(console, "truncated", False)),
            test_report=test_report if isinstance(test_report, dict) else None,
            build_history=history if isinstance(history, list) else [],
            last_success=last_success if isinstance(last_success, dict) else None,
            last_success_test_report=(
                last_success_test_report if isinstance(last_success_test_report, dict) else None
            ),
        )
        evidence_items = _initial_evidence_items(state)
        source = redact(f"jenkins:{event.job_name}#{event.build_number}")
        evidence_items.append(
            EvidenceItem(
                id="build_summary",
                kind="test_result",
                source=source,
                timestamp=event.timestamp,
                content=_json_text(build_summary),
            )
        )
        if isinstance(test_report, dict):
            evidence_items.append(
                EvidenceItem(
                    id="test_report",
                    kind="test_result",
                    source=source,
                    timestamp=event.timestamp,
                    content=_json_text(test_report),
                )
            )
        if history:
            evidence_items.append(
                EvidenceItem(
                    id="build_history",
                    kind="test_result",
                    source=source,
                    timestamp=event.timestamp,
                    content=_json_text(history[:30]),
                )
            )
        if last_success:
            evidence_items.append(
                EvidenceItem(
                    id="last_success",
                    kind="commit",
                    source=source,
                    timestamp=event.timestamp,
                    content=_json_text(_selected_build_fields(last_success)),
                )
            )
        if isinstance(last_success_test_report, dict):
            evidence_items.append(
                EvidenceItem(
                    id="last_success_test_report",
                    kind="test_result",
                    source=source,
                    timestamp=event.timestamp,
                    content=_json_text(last_success_test_report),
                )
            )
        state["evidence_items"] = evidence_items
        llm_evidence_ids = [
            item.id
            for item in evidence_items
            if not item.id.startswith("failing_tests[")
        ]
        llm_evidence_ids.extend(
            f"failing_tests[{index}]"
            for index in range(min(len(parsed.failing_tests), MAX_TEST_FAILURES_IN_PROMPT))
        )
        context = {
            "incident_id": str(event.incident_id),
            "job_name": event.job_name,
            "build_number": event.build_number,
            "branch": event.branch,
            "git_commit": event.git_commit,
            "failed_stage": failed_stage or parsed.failed_stage,
            "build_summary": build_summary,
            "stages": [
                {"name": stage.name, "result": stage.result, "duration_ms": stage.duration_ms}
                for stage in parsed.stages
            ],
            "hypotheses": [
                {
                    "failure_type": hypothesis.failure_type.value,
                    "reason": hypothesis.reason,
                    "matched_evidence_ids": (
                        hypothesis.matched_evidence_ids[:MAX_TEST_FAILURES_IN_PROMPT]
                        if hypothesis.failure_type is FailureTaxonomy.CODE_TEST_FAILURE
                        else hypothesis.matched_evidence_ids
                    ),
                    "rule_confidence": hypothesis.rule_confidence,
                }
                for hypothesis in hypotheses
            ],
            "failing_test_summary": _failing_test_summary(parsed),
            "evidence_ids": llm_evidence_ids,
            "last_success_test_report": last_success_test_report,
        }
        state["messages"] = [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=_json_text(context, limit=24_000)),
        ]
        state["steps_used"] = 0
        state["tool_counts"] = {}
        state["validation_retries"] = 0
        state["continue_reasoning"] = True
        state["llm_failed"] = False
        return state

    async def _invoke_model(self, model: Any, messages: Sequence[BaseMessage]) -> AIMessage:
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            try:
                response = await asyncio.wait_for(
                    model.ainvoke(_safe_messages(messages)), timeout=self.llm_timeout
                )
                if not isinstance(response, AIMessage):
                    response = AIMessage(content=str(response))
                return response
            except Exception as error:
                if not _is_rate_limited(error) or attempt >= MAX_RATE_LIMIT_RETRIES:
                    raise
                await self.sleep(min(2**attempt, 8))
        raise AssertionError("unreachable")

    async def reason(self, state: AgentState) -> dict[str, Any]:
        """Perform one bounded ReAct turn, executing only registered read-only tools."""
        if state.get("steps_used", 0) >= MAX_STEPS:
            state["continue_reasoning"] = False
            return state
        tools = _tools_for_state(state)
        model = self.llm_client.bind_tools(tools)
        state["steps_used"] = state.get("steps_used", 0) + 1
        try:
            response = await self._invoke_model(model, state.get("messages", []))
        except Exception as error:
            state["llm_failed"] = True
            state["continue_reasoning"] = False
            logger.warning("LLM reasoning failed; using rule-based hypotheses", extra={"error": _error_text(error)})
            return state

        safe_response = AIMessage(
            content=_redact_value(response.content),
            tool_calls=[
                {
                    "name": call["name"],
                    "args": _redact_value(call.get("args", {})),
                    "id": call.get("id", ""),
                    "type": "tool_call",
                }
                for call in response.tool_calls
            ],
        )
        messages = state.setdefault("messages", [])
        messages.append(safe_response)
        if safe_response.tool_calls:
            await self._execute_tool_calls(state, safe_response, tools)
            state["continue_reasoning"] = True
            return state

        try:
            answer = LLMAnswer.model_validate_json(_content_as_text(safe_response))
            evidence = _evidence_from_answer(state, answer)
        except (ValidationError, ValueError, json.JSONDecodeError) as error:
            retry_count = state.get("validation_retries", 0)
            if retry_count == 0 and state.get("steps_used", 0) < MAX_STEPS:
                state["validation_retries"] = retry_count + 1
                messages.append(
                    HumanMessage(
                        content=redact(
                            "Your previous final answer failed JSON or Evidence validation: "
                            f"{error}. Retry once with valid JSON and citations to existing evidence IDs."
                        )
                    )
                )
                state["continue_reasoning"] = True
            else:
                state["continue_reasoning"] = False
            return state

        state["evidence"] = evidence
        state["continue_reasoning"] = False
        return state

    async def _execute_tool_calls(
        self,
        state: AgentState,
        response: AIMessage,
        tools: Sequence[StructuredTool],
    ) -> None:
        registry = {tool.name: tool for tool in tools}
        messages = state.setdefault("messages", [])
        calls = state.setdefault("tool_calls", [])
        counts = state.setdefault("tool_counts", {})
        for call in response.tool_calls:
            name = call["name"]
            args = call.get("args", {})
            tool = registry.get(name)
            counts[name] = counts.get(name, 0) + 1
            started = time.perf_counter()
            ok = False
            observation = ""
            if tool is None:
                observation = f"Unknown tool: {name}"
            elif counts[name] > MAX_CALLS_PER_TOOL:
                observation = f"Tool call limit reached for {name} ({MAX_CALLS_PER_TOOL})."
            else:
                try:
                    result = await asyncio.wait_for(
                        tool.ainvoke(args), timeout=self.per_call_timeout
                    )
                    observation = redact(str(result))
                    ok = True
                except Exception as error:
                    observation = f"Tool error: {_error_text(error)}"
            duration_ms = (time.perf_counter() - started) * 1000
            calls.append(
                ToolCallRecord(
                    tool=name,
                    args=_redact_value(args),
                    duration_ms=duration_ms,
                    ok=ok,
                )
            )
            if ok:
                evidence_id = f"tool_observation[{len(calls) - 1}]"
                state.setdefault("evidence_items", []).append(
                    EvidenceItem(
                        id=evidence_id,
                        kind="log_excerpt",
                        source=redact(
                            f"jenkins:{state['event'].job_name}#{state['event'].build_number}"
                        ),
                        timestamp=state["event"].timestamp,
                        content=observation,
                    )
                )
                observation = f"Evidence ID: {evidence_id}\n{observation}"
            messages.append(
                ToolMessage(content=redact(observation), tool_call_id=call.get("id", ""), name=name)
            )

    def _next_node(self, state: AgentState) -> str:
        if state.get("evidence") is not None:
            return "finalize"
        if not state.get("continue_reasoning") or state.get("steps_used", 0) >= MAX_STEPS:
            return "finalize"
        return "reason"

    async def finalize(self, state: AgentState) -> dict[str, Any]:
        """Validate fallback evidence if needed, then persist before returning."""
        evidence = state.get("evidence")
        if evidence is None:
            evidence = _fallback_evidence(state)
        evidence = Evidence.model_validate(evidence.model_dump())
        await asyncio.wait_for(self.evidence_store.write(evidence), timeout=self.per_call_timeout)
        state["evidence"] = evidence
        return state

    async def run_event(self, event: IncidentCreatedEvent) -> Evidence:
        """Run the graph under a total deadline and persist evidence before success."""
        state: AgentState = {
            "incident_id": str(event.incident_id),
            "event": event,
            "parsed": ParsedBuild(),
            "hypotheses": [],
            "messages": [],
            "steps_used": 0,
            "evidence": None,
            "evidence_items": [],
            "tool_calls": [],
            "tool_counts": {},
            "validation_retries": 0,
            "continue_reasoning": True,
            "llm_failed": False,
            "last_success_test_report": None,
        }
        try:
            result = await asyncio.wait_for(self.graph.ainvoke(state), timeout=self.total_timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "Jenkins investigation exceeded total timeout",
                extra={"incident_id": str(event.incident_id)},
            )
            fallback = _fallback_evidence(state, status="insufficient_evidence")
            await asyncio.wait_for(self.evidence_store.write(fallback), timeout=self.per_call_timeout)
            return fallback
        evidence = result.get("evidence")
        if not isinstance(evidence, Evidence):
            raise RuntimeError("graph completed without persisted Evidence")
        return evidence


def _initial_evidence_items(state: AgentState) -> list[EvidenceItem]:
    event = state["event"]
    parsed = state["parsed"]
    source = redact(f"jenkins:{event.job_name}#{event.build_number}")
    items: list[EvidenceItem] = []
    for index, stage in enumerate(parsed.stages):
        items.append(
            EvidenceItem(
                id=f"stages[{index}]",
                kind="test_result",
                source=source,
                timestamp=event.timestamp,
                content=_json_text(
                    {"name": stage.name, "result": stage.result, "duration_ms": stage.duration_ms}
                ),
            )
        )
    if parsed.failed_stage:
        items.append(
            EvidenceItem(
                id="failed_stage",
                kind="test_result",
                source=source,
                timestamp=event.timestamp,
                content=redact(parsed.failed_stage),
            )
        )
    for index, test in enumerate(parsed.failing_tests):
        items.append(
            EvidenceItem(
                id=f"failing_tests[{index}]",
                kind="test_result",
                source=source,
                timestamp=event.timestamp,
                content=_json_text(
                    {"name": test.name, "file": test.file, "line": test.line, "message": test.message}
                ),
                location={"file": redact(test.file), "line": test.line} if test.file else None,
            )
        )
    for index, error in enumerate(parsed.compiler_errors):
        items.append(
            EvidenceItem(
                id=f"compiler_errors[{index}]",
                kind="log_excerpt",
                source=source,
                timestamp=event.timestamp,
                content=redact(error.message),
                location={"file": redact(error.file), "line": error.line} if error.file else None,
            )
        )
    for index, block in enumerate(parsed.error_blocks):
        items.append(
            EvidenceItem(
                id=f"error_blocks[{index}]",
                kind="log_excerpt",
                source=source,
                timestamp=event.timestamp,
                content=redact(block),
            )
        )
    for index, timeout in enumerate(parsed.timeouts):
        items.append(
            EvidenceItem(
                id=f"timeouts[{index}]",
                kind="log_excerpt",
                source=source,
                timestamp=event.timestamp,
                content=redact(timeout),
            )
        )
    for index, signal in enumerate(parsed.auth_signals):
        items.append(
            EvidenceItem(
                id=f"auth_signals[{index}]",
                kind="log_excerpt",
                source=source,
                timestamp=event.timestamp,
                content=redact(signal),
            )
        )
    if parsed.error_signature:
        items.append(
            EvidenceItem(
                id="error_signature",
                kind="log_excerpt",
                source=source,
                timestamp=event.timestamp,
                content=parsed.error_signature,
            )
        )
    return items


def _evidence_from_answer(state: AgentState, answer: LLMAnswer) -> Evidence:
    event = state["event"]
    parsed = state["parsed"]
    status = answer.status if answer.status in {"completed", "failed", "insufficient_evidence"} else "completed"
    rule_hypotheses = state.get("hypotheses") or rule_based_classify(parsed)
    supported_types = {hypothesis.failure_type for hypothesis in rule_hypotheses}
    failure_type = answer.failure_type
    classification_overridden = False
    if (
        parsed.failing_tests
        and rule_hypotheses[0].failure_type is FailureTaxonomy.CODE_TEST_FAILURE
    ):
        classification_overridden = answer.failure_type is not FailureTaxonomy.CODE_TEST_FAILURE
        failure_type = FailureTaxonomy.CODE_TEST_FAILURE
        rule_hypotheses = [rule_hypotheses[0]]
    elif (
        rule_hypotheses[0].failure_type is not FailureTaxonomy.UNKNOWN
        and answer.failure_type not in supported_types
    ):
        failure_type = rule_hypotheses[0].failure_type
        classification_overridden = True

    summary = redact(answer.summary)
    recommended_next_steps = [redact(step) for step in answer.recommended_next_steps]
    evidence_ids = {item.id for item in state.get("evidence_items", [])}
    has_environment_hypothesis = any(
        any(
            term in hypothesis.hypothesis.casefold()
            for term in ("environment", "infrastructure", "network")
        )
        for hypothesis in answer.root_cause_hypotheses
    )
    if classification_overridden or (
        failure_type is FailureTaxonomy.CODE_TEST_FAILURE
        and parsed.failing_tests
        and has_environment_hypothesis
    ):
        root_cause_hypotheses = [
            EvidenceHypothesis(
                hypothesis=redact(hypothesis.reason),
                failure_type=hypothesis.failure_type,
                confidence=hypothesis.rule_confidence,
                supporting_evidence=[
                    evidence_id
                    for evidence_id in hypothesis.matched_evidence_ids
                    if evidence_id in evidence_ids
                ],
            )
            for hypothesis in rule_hypotheses
        ]
    else:
        root_cause_hypotheses = [
            hypothesis.model_copy(
                update={
                    "hypothesis": redact(hypothesis.hypothesis),
                    "supporting_evidence": [redact(item) for item in hypothesis.supporting_evidence],
                    "contradicting_evidence": [redact(item) for item in hypothesis.contradicting_evidence],
                }
            )
            for hypothesis in answer.root_cause_hypotheses
        ]

    if failure_type is FailureTaxonomy.CODE_TEST_FAILURE and parsed.failing_tests:
        failing_test = parsed.failing_tests[0]
        location = _format_location(failing_test.file, failing_test.line)
        factual_summary = (
            f"Failing test {failing_test.name}"
            + (f" in {location}" if location else "")
        )
        if any(term in summary.casefold() for term in ("environment", "infrastructure", "network")):
            summary = f"A test assertion failed. {factual_summary}."
        else:
            summary = _append_fact(summary, factual_summary)
        summary = _append_test_failure_summary(summary, parsed)
        recommendation = "Correct the implementation/test."
        if not any("correct the implementation/test" in step.casefold() for step in recommended_next_steps):
            recommended_next_steps.insert(0, recommendation)
    elif failure_type is FailureTaxonomy.BUILD_COMPILATION_FAILURE:
        compile_error = next(
            (
                error for error in parsed.compiler_errors
                if error.file or error.line
            ),
            None,
        )
        if compile_error is not None:
            location = _format_location(compile_error.file, compile_error.line)
            if location:
                summary = _append_fact(summary, f"Compiler error at {location}")

    if state.get("log_truncated"):
        recommended_next_steps.append(
            "Review the truncated build log before treating this classification as complete."
        )

    return Evidence.model_validate(
        {
            "incident_id": event.incident_id,
            "created_at": datetime.now(timezone.utc),
            "status": status,
            "failure_type": failure_type,
            "summary": summary,
            "root_cause_hypotheses": root_cause_hypotheses,
            "evidence_items": state.get("evidence_items", []),
            "tool_calls": state.get("tool_calls", []),
            "confidence": min(
                answer.confidence,
                rule_hypotheses[0].rule_confidence if classification_overridden else 1.0,
                0.6 if state.get("log_truncated") else 1.0,
            ),
            "recommended_next_steps": recommended_next_steps,
            "failed_stage": redact(event.failed_stage or parsed.failed_stage) if (event.failed_stage or parsed.failed_stage) else None,
            "failing_tests": [redact(test.name) for test in parsed.failing_tests],
            "error_signature": parsed.error_signature,
            "log_truncated": state.get("log_truncated", False),
            "redaction_applied": True,
            "flaky_score": next(
                (hypothesis.confidence for hypothesis in answer.root_cause_hypotheses
                 if hypothesis.failure_type is FailureTaxonomy.FLAKY_TEST),
                None,
            ),
        }
    )


def _fallback_evidence(state: AgentState, *, status: str | None = None) -> Evidence:
    event = state["event"]
    parsed = state.get("parsed", ParsedBuild())
    hypotheses = state.get("hypotheses") or rule_based_classify(parsed)
    evidence_items = state.get("evidence_items") or _initial_evidence_items(
        {"event": event, "parsed": parsed}
    )
    evidence_ids = {item.id for item in evidence_items}
    primary = hypotheses[0]
    is_unknown = primary.failure_type is FailureTaxonomy.UNKNOWN
    root_hypotheses = [
        EvidenceHypothesis(
            hypothesis=redact(hypothesis.reason),
            failure_type=hypothesis.failure_type,
            confidence=hypothesis.rule_confidence,
            supporting_evidence=[item for item in hypothesis.matched_evidence_ids if item in evidence_ids],
        )
        for hypothesis in hypotheses
    ]
    confidence = max((hypothesis.rule_confidence for hypothesis in hypotheses), default=0.2)
    if state.get("log_truncated"):
        confidence = min(confidence, 0.6)
    summary = "Insufficient evidence to identify a specific failure cause." if is_unknown else redact(primary.reason)
    recommended_next_steps = (
        ["Collect additional build evidence and review the related change before remediation."]
        if is_unknown
        else ["Review the cited evidence and confirm the suspected cause before remediation."]
    )
    if primary.failure_type is FailureTaxonomy.CODE_TEST_FAILURE and parsed.failing_tests:
        failing_test = parsed.failing_tests[0]
        location = _format_location(failing_test.file, failing_test.line)
        summary = _append_fact(
            summary,
            f"Failing test {failing_test.name}"
            + (f" in {location}" if location else ""),
        )
        summary = _append_test_failure_summary(summary, parsed)
        recommended_next_steps.insert(0, "Correct the implementation/test.")
    elif primary.failure_type is FailureTaxonomy.BUILD_COMPILATION_FAILURE:
        compile_error = next(
            (error for error in parsed.compiler_errors if error.file or error.line),
            None,
        )
        if compile_error is not None:
            location = _format_location(compile_error.file, compile_error.line)
            if location:
                summary = _append_fact(summary, f"Compiler error at {location}")
    if state.get("log_truncated"):
        recommended_next_steps.append(
            "Review the truncated build log before treating this classification as complete."
        )
    return Evidence.model_validate(
        {
            "incident_id": event.incident_id,
            "created_at": datetime.now(timezone.utc),
            "status": status or ("insufficient_evidence" if is_unknown else "completed"),
            "failure_type": primary.failure_type,
            "summary": summary,
            "root_cause_hypotheses": root_hypotheses,
            "evidence_items": evidence_items,
            "tool_calls": state.get("tool_calls", []),
            "confidence": confidence,
            "recommended_next_steps": recommended_next_steps,
            "failed_stage": redact(event.failed_stage or parsed.failed_stage)
            if (event.failed_stage or parsed.failed_stage)
            else None,
            "failing_tests": [redact(test.name) for test in parsed.failing_tests],
            "error_signature": parsed.error_signature,
            "log_truncated": state.get("log_truncated", False),
            "redaction_applied": True,
            "flaky_score": next(
                (hypothesis.rule_confidence for hypothesis in hypotheses
                 if hypothesis.failure_type is FailureTaxonomy.FLAKY_TEST),
                None,
            ),
        }
    )


def _format_location(file: str | None, line: int | None) -> str:
    location = file or ""
    if line is not None:
        location = f"{location}:{line}" if location else f"line {line}"
    return location


def _append_fact(summary: str, fact: str) -> str:
    if fact.casefold() in summary.casefold():
        return summary
    return f"{summary.rstrip()} {fact}."


def parse_event(body: bytes) -> IncidentCreatedEvent:
    """Decode and validate a RabbitMQ `incident.created` event."""
    return IncidentCreatedEvent.model_validate_json(body)


async def run(settings: JenkinsAgentSettings) -> None:
    """Connect the worker, declare a bounded-retry queue, and consume incidents."""
    connection = await aio_pika.connect_robust(
        settings.RABBITMQ_URL.get_secret_value(), timeout=10
    )
    store = FileEvidenceStore()
    try:
        agent = JenkinsInvestigationAgent(
            LLMClient(
                api_key=(
                    settings.GEMINI_API_KEY.get_secret_value()
                    if settings.GEMINI_API_KEY
                    else None
                ),
                model_name=settings.GEMINI_MODEL,
                provider=settings.LLM_PROVIDER,
                timeout_seconds=settings.LLM_TIMEOUT_SECONDS,
                requests_per_minute=settings.LLM_REQUESTS_PER_MINUTE,
                offline=settings.LLM_OFFLINE,
            ),
            store,
            per_call_timeout=settings.PER_CALL_TIMEOUT_SECONDS,
            total_timeout=settings.TOTAL_TIMEOUT_SECONDS,
            llm_timeout=settings.LLM_TIMEOUT_SECONDS,
        )
        async with connection:
            channel = await connection.channel()
            await channel.set_qos(prefetch_count=1)
            exchange = await channel.declare_exchange(
                settings.EXCHANGE_NAME, aio_pika.ExchangeType.TOPIC, durable=True
            )
            dead_exchange = await channel.declare_exchange(
                f"{settings.EXCHANGE_NAME}.dead", aio_pika.ExchangeType.DIRECT, durable=True
            )
            queue = await channel.declare_queue(
                settings.QUEUE_NAME,
                durable=True,
                arguments={
                    "x-queue-type": "quorum",
                    "x-delivery-limit": 3,
                    "x-dead-letter-exchange": f"{settings.EXCHANGE_NAME}.dead",
                    "x-dead-letter-routing-key": f"{settings.QUEUE_NAME}.dead",
                },
            )
            dead_queue = await channel.declare_queue(f"{settings.QUEUE_NAME}.dead", durable=True)
            await queue.bind(exchange, routing_key=settings.ROUTING_KEY)
            await dead_queue.bind(dead_exchange, routing_key=f"{settings.QUEUE_NAME}.dead")
            logger.info("Jenkins agent consuming", extra={"queue": settings.QUEUE_NAME})
            async with queue.iterator() as messages:
                async for message in messages:
                    await _process_message(message, agent)
    finally:
        if not connection.is_closed:
            await connection.close()


async def _process_message(message: AbstractIncomingMessage, agent: JenkinsInvestigationAgent) -> None:
    try:
        event = parse_event(message.body)
    except (ValueError, ValidationError, json.JSONDecodeError):
        logger.error("Dropping malformed incident event")
        await message.nack(requeue=False)
        return
    try:
        evidence = await agent.run_event(event)
        if not evidence.redaction_applied:
            raise ValueError("agent returned evidence without redaction")
    except Exception:
        logger.error(
            "Jenkins incident processing failed",
            extra={"incident_id": str(event.incident_id)},
        )
        await message.nack(requeue=True)
        return
    await message.ack()


def main() -> None:
    settings = load_settings()
    logging.basicConfig(
        level=settings.LOG_LEVEL,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(run(settings))


if __name__ == "__main__":
    main()
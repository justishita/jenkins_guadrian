"""CrewAI Jenkins build investigator and worker."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import time
from typing import Any, Callable, Sequence
from uuid import UUID

import aio_pika
from crewai import Agent, Crew, Process, Task
from pydantic import BaseModel, ConfigDict, Field
import yaml

from agents.jenkins_agent.classifier import Hypothesis, rule_based_classify
from agents.jenkins_agent.config import JenkinsAgentSettings, load_settings
from agents.jenkins_agent.parsing import ParsedBuild, parse_build
from agents.jenkins_agent.tools.jenkins_client import JenkinsClient
from common.blackboard import (
    BlackboardEntry,
    ErrorSignatureHint,
    FailedStageHint,
    FailureWindowHint,
    FileBlackboard,
    FileEvidenceStore,
    SuspectFilesHint,
    get_blackboard,
)
from common.a2a_contract import evidence_ready
from common.crew_llm import CrewLLMAdapter
from common.guardrails import evidence_guardrail, screen_untrusted_text
from common.llm_client import LLMClient
from common.mcp_client import get_analytics_mcp_tools, get_jenkins_mcp_tools
from common.models import (
    Evidence,
    EvidenceHypothesis,
    EvidenceItem,
    FailureTaxonomy,
    IncidentCreatedEvent,
    ToolCallRecord,
)
from common.monitoring import create_step_callback, create_task_callback
from common.redaction import redact, redact_value

logger = logging.getLogger(__name__)

MAX_STEPS = 8
MAX_CALLS_PER_TOOL = 3
MAX_TEST_FAILURES_IN_PROMPT = 5


class EvidenceDraft(BaseModel):
    """Pydantic structured output model for CrewAI Task."""

    model_config = ConfigDict(extra="ignore")

    failure_type: FailureTaxonomy = FailureTaxonomy.UNKNOWN
    summary: str = Field(min_length=1, default="Build failure investigated")
    root_cause_hypotheses: list[EvidenceHypothesis] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    recommended_next_steps: list[str] = Field(default_factory=list)


def _load_prompt_variant(variant_name: str | None = None) -> dict[str, Any]:
    variant = variant_name or os.getenv("PROMPT_VARIANT", "react")
    prompt_file = Path(__file__).parent / "prompts" / f"{variant}.yaml"
    if not prompt_file.exists():
        prompt_file = Path(__file__).parent / "prompts" / "react.yaml"
    try:
        with open(prompt_file, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as error:
        logger.warning("Failed to load prompt variant (%s)", type(error).__name__)
        return {
            "name": "react",
            "role": "CI failure analyst",
            "goal": "Accurately identify root causes of CI build failures",
            "backstory": "You are a CI failure analyst investigating one Jenkins incident.",
            "reasoning": False,
        }


def _initial_evidence_items(
    event: IncidentCreatedEvent,
    parsed: ParsedBuild,
    build_summary: dict[str, Any],
    test_report: dict[str, Any] | None,
    history: list[dict[str, Any]],
) -> list[EvidenceItem]:
    source = redact(f"jenkins:{event.job_name}#{event.build_number}")
    items: list[EvidenceItem] = []

    items.append(
        EvidenceItem(
            id="build_summary",
            kind="test_result",
            source=source,
            timestamp=event.timestamp,
            content=json.dumps(redact_value(build_summary)),
        )
    )

    for index, stage in enumerate(parsed.stages):
        items.append(
            EvidenceItem(
                id=f"stages[{index}]",
                kind="test_result",
                source=source,
                timestamp=event.timestamp,
                content=json.dumps(
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

    for index, test in enumerate(parsed.failing_tests[:MAX_TEST_FAILURES_IN_PROMPT]):
        items.append(
            EvidenceItem(
                id=f"failing_tests[{index}]",
                kind="test_result",
                source=source,
                timestamp=event.timestamp,
                content=json.dumps(
                    redact_value(
                        {
                            "name": test.name,
                            "file": test.file,
                            "line": test.line,
                            "message": test.message,
                        }
                    )
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

    if test_report:
        items.append(
            EvidenceItem(
                id="test_report",
                kind="test_result",
                source=source,
                timestamp=event.timestamp,
                content=json.dumps(redact_value(test_report)),
            )
        )

    if history:
        items.append(
            EvidenceItem(
                id="build_history",
                kind="test_result",
                source=source,
                timestamp=event.timestamp,
                content=json.dumps(redact_value(history[:30])),
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


def _fallback_evidence(
    event: IncidentCreatedEvent,
    parsed: ParsedBuild,
    hypotheses: list[Hypothesis],
    evidence_items: list[EvidenceItem],
    tool_calls: list[ToolCallRecord],
    status: str = "completed",
    log_truncated: bool = False,
    partial: bool = False,
) -> Evidence:
    """Build deterministic rule-based Evidence when LLM is unavailable or fails."""
    primary = hypotheses[0] if hypotheses else None
    failure_type = primary.failure_type if primary else FailureTaxonomy.UNKNOWN
    confidence_adjustment = 0.75 if log_truncated or partial else 1.0
    confidence = (primary.rule_confidence if primary else 0.2) * confidence_adjustment

    valid_evidence_ids = {item.id for item in evidence_items}
    root_cause_hypotheses: list[EvidenceHypothesis] = []

    for hyp in hypotheses[:3]:
        matched = [eid for eid in hyp.matched_evidence_ids if eid in valid_evidence_ids]
        root_cause_hypotheses.append(
            EvidenceHypothesis(
                hypothesis=hyp.reason,
                failure_type=hyp.failure_type,
                confidence=hyp.rule_confidence * confidence_adjustment,
                supporting_evidence=matched,
                contradicting_evidence=[],
            )
        )

    if not root_cause_hypotheses:
        root_cause_hypotheses.append(
            EvidenceHypothesis(
                hypothesis="Insufficient evidence to determine exact failure cause",
                failure_type=FailureTaxonomy.UNKNOWN,
                confidence=0.2 * confidence_adjustment,
                supporting_evidence=[item.id for item in evidence_items[:2]],
                contradicting_evidence=[],
            )
        )

    return Evidence(
        incident_id=event.incident_id,
        agent="jenkins_agent",
        created_at=datetime.now(timezone.utc),
        status=status,
        failure_type=failure_type,
        summary=f"Rule-based classification: {failure_type.value}",
        root_cause_hypotheses=root_cause_hypotheses,
        evidence_items=evidence_items,
        tool_calls=tool_calls,
        confidence=confidence,
        recommended_next_steps=["Inspect CI logs at point of failure"],
        failed_stage=parsed.failed_stage,
        failing_tests=[
            redact(t.name) for t in parsed.failing_tests[:MAX_TEST_FAILURES_IN_PROMPT]
        ],
        log_truncated=log_truncated,
        error_signature=parsed.error_signature,
        redaction_applied=True,
    )


class JenkinsInvestigationAgent:
    """CrewAI Jenkins build investigator and evidence generator."""

    def __init__(
        self,
        llm_client: LLMClient | None = None,
        evidence_store: Any = None,
        *,
        jenkins_client_factory: Callable[[], JenkinsClient] = JenkinsClient,
        per_call_timeout: float = 20.0,
        total_timeout: float = 180.0,
        llm_timeout: float = 30.0,
        sleep: Callable[[float], Any] = asyncio.sleep,
        notifier: Callable[[Any], Awaitable[None]] | None = None,
    ) -> None:
        self.llm_client = llm_client or LLMClient()
        self.evidence_store = evidence_store or FileEvidenceStore()
        self.jenkins_client_factory = jenkins_client_factory
        self.per_call_timeout = per_call_timeout
        self.total_timeout = total_timeout
        self.llm_timeout = llm_timeout
        self.sleep = sleep
        self.notifier = notifier
        self.bb = get_blackboard()

    async def run_event(self, event: IncidentCreatedEvent) -> Evidence:
        """Execute full investigation: fetch context -> post hints -> reason -> finalize."""
        started_time = time.perf_counter()
        tool_calls: list[ToolCallRecord] = []
        client = self.jenkins_client_factory()

        # 1. Fetch Context
        console_text = ""
        console_truncated = False
        console_partial = False
        stage_summary: dict[str, Any] = {}
        test_report: dict[str, Any] | None = None
        history: list[dict[str, Any]] = []

        try:
            console_resp = await asyncio.wait_for(
                client.get_console_text(event.job_name, event.build_number),
                timeout=self.per_call_timeout,
            )
            console_text = console_resp.text
            console_truncated = console_resp.truncated
            console_partial = console_resp.partial
            tool_calls.append(
                ToolCallRecord(tool="get_console_text", duration_ms=10.0, ok=True)
            )
        except Exception as error:
            logger.warning("Jenkins console collection failed (%s)", type(error).__name__)
            tool_calls.append(
                ToolCallRecord(
                    tool="get_console_text",
                    duration_ms=10.0,
                    ok=False,
                )
            )

        try:
            stages_resp = await asyncio.wait_for(
                client.get_stage_summary(event.job_name, event.build_number),
                timeout=self.per_call_timeout,
            )
            stage_summary = stages_resp if isinstance(stages_resp, dict) else {}
            tool_calls.append(
                ToolCallRecord(tool="get_stage_summary", duration_ms=10.0, ok=True)
            )
        except Exception as error:
            logger.warning("Jenkins stage collection failed (%s)", type(error).__name__)
            tool_calls.append(
                ToolCallRecord(
                    tool="get_stage_summary",
                    duration_ms=10.0,
                    ok=False,
                )
            )

        try:
            test_report = await asyncio.wait_for(
                client.get_test_report(event.job_name, event.build_number),
                timeout=self.per_call_timeout,
            )
        except Exception:
            pass

        try:
            history = await asyncio.wait_for(
                client.get_build_history(event.job_name),
                timeout=self.per_call_timeout,
            )
        except Exception:
            pass

        close_client = getattr(client, "aclose", None)
        if close_client is not None:
            await close_client()

        # Screen untrusted console text
        screened = screen_untrusted_text(
            console_text,
            source="console_log",
            incident_id=event.incident_id,
        )

        # Parse build
        parsed = parse_build(
            screened.text,
            stage_summary=stage_summary,
            failed_stage=event.failed_stage,
        )

        # Rule-based classify
        rule_hypotheses = rule_based_classify(parsed)

        # Build initial evidence items
        build_summary = {
            "job_name": redact(event.job_name),
            "build_number": event.build_number,
            "console_truncated": console_truncated,
            "console_partial": console_partial,
            "total_failing_tests": len(parsed.failing_tests),
            "failing_tests_included": min(
                len(parsed.failing_tests), MAX_TEST_FAILURES_IN_PROMPT
            ),
        }
        evidence_items = _initial_evidence_items(event, parsed, build_summary, test_report, history)

        # Suspect files
        suspect_files = [t.file for t in parsed.failing_tests if t.file] + [
            c.file for c in parsed.compiler_errors if c.file
        ]
        suspect_files = list(dict.fromkeys(suspect_files))

        # Immediately post hints to Blackboard
        try:
            # failure_window
            self.bb.post(
                BlackboardEntry(
                    incident_id=event.incident_id,
                    section="hints",
                    key="jenkins_agent",
                    author="jenkins_agent",
                    payload={
                        "start": event.timestamp.isoformat(),
                        "end": datetime.now(timezone.utc).isoformat(),
                    },
                )
            )
            # failed_stage
            if parsed.failed_stage:
                self.bb.post(
                    BlackboardEntry(
                        incident_id=event.incident_id,
                        section="hints",
                        key="jenkins_agent",
                        author="jenkins_agent",
                        payload={"failed_stage": parsed.failed_stage},
                    )
                )
            # error_signature
            if parsed.error_signature:
                self.bb.post(
                    BlackboardEntry(
                        incident_id=event.incident_id,
                        section="hints",
                        key="jenkins_agent",
                        author="jenkins_agent",
                        payload={"error_signature": parsed.error_signature},
                    )
                )
            # suspect_files
            if suspect_files:
                self.bb.post(
                    BlackboardEntry(
                        incident_id=event.incident_id,
                        section="hints",
                        key="jenkins_agent",
                        author="jenkins_agent",
                        payload={"suspect_files": suspect_files},
                    )
                )
        except Exception as e:
            logger.warning("Failed to post hints to Blackboard (%s)", type(e).__name__)

        # 2. Reason via CrewAI (or fallback if offline/failed)
        offline = os.getenv("LLM_OFFLINE", "0") == "1"
        draft: EvidenceDraft | None = None

        if not offline:
            try:
                variant_config = _load_prompt_variant()
                step_cb = create_step_callback(
                    agent_name="jenkins_agent",
                    incident_id=str(event.incident_id),
                    variant=variant_config.get("name", "react"),
                )
                task_cb = create_task_callback(
                    agent_name="jenkins_agent",
                    incident_id=str(event.incident_id),
                    variant=variant_config.get("name", "react"),
                )

                tools = (
                    get_jenkins_mcp_tools(event.job_name, event.build_number)
                    + get_analytics_mcp_tools()
                )

                llm_adapter = CrewLLMAdapter(self.llm_client)

                agent = Agent(
                    role=variant_config.get("role", "CI failure analyst"),
                    goal=variant_config.get("goal", "Accurately identify root causes"),
                    backstory=variant_config.get("backstory", "You are a CI failure analyst."),
                    tools=tools,
                    llm=llm_adapter,
                    max_iter=MAX_STEPS,
                    allow_delegation=False,
                    step_callback=step_cb,
                )

                valid_eids = [item.id for item in evidence_items]
                screened_job_name = screen_untrusted_text(
                    event.job_name,
                    source="jenkins_job_name",
                ).text
                screened_failed_stage = screen_untrusted_text(
                    parsed.failed_stage or "unknown",
                    source="jenkins_stage",
                ).text
                screened_error_signature = screen_untrusted_text(
                    parsed.error_signature or "unknown",
                    source="jenkins_error_signature",
                ).text
                task_desc = (
                    f"Investigate Jenkins failure for {screened_job_name} #{event.build_number}.\n"
                    f"Failed Stage: {screened_failed_stage}\n"
                    f"Error Signature: {screened_error_signature}\n"
                    f"Console truncated: {console_truncated}; partial build: {console_partial}.\n"
                    f"Failing tests: {len(parsed.failing_tests)} total; "
                    f"showing at most {MAX_TEST_FAILURES_IN_PROMPT}.\n"
                    f"Rule Hypotheses: {[h.reason for h in rule_hypotheses]}\n"
                    f"Available Evidence IDs: {valid_eids}\n"
                    f"Ground every claim in a cited evidence ID. If evidence is insufficient, say so with low confidence."
                )

                task = Task(
                    description=task_desc,
                    expected_output="An EvidenceDraft structured object diagnosing the failure.",
                    output_pydantic=EvidenceDraft,
                    guardrail=evidence_guardrail,
                    guardrail_max_retries=1,
                    callback=task_cb,
                    agent=agent,
                )

                crew = Crew(
                    agents=[agent],
                    tasks=[task],
                    process=Process.sequential,
                    memory=False,
                )

                result = await asyncio.wait_for(
                    asyncio.to_thread(crew.kickoff),
                    timeout=min(self.llm_timeout, 60.0),
                )

                if hasattr(result, "pydantic") and isinstance(result.pydantic, EvidenceDraft):
                    draft = result.pydantic

            except Exception as exc:
                logger.warning("CrewAI execution fallback triggered (%s)", type(exc).__name__)
                draft = None

        # 3. Finalize
        if draft is not None:
            # Ensure cited evidence IDs exist
            valid_ids = {item.id for item in evidence_items}
            sanitized_hypotheses: list[EvidenceHypothesis] = []
            for h in draft.root_cause_hypotheses:
                supporting = [eid for eid in h.supporting_evidence if eid in valid_ids]
                if not supporting and valid_ids:
                    supporting = [list(valid_ids)[0]]
                sanitized_hypotheses.append(
                    EvidenceHypothesis(
                        hypothesis=h.hypothesis,
                        failure_type=h.failure_type,
                        confidence=h.confidence
                        * (0.75 if console_truncated or console_partial else 1.0),
                        supporting_evidence=supporting,
                        contradicting_evidence=[],
                    )
                )

            if not sanitized_hypotheses and rule_hypotheses:
                sanitized_hypotheses.append(
                    EvidenceHypothesis(
                        hypothesis=rule_hypotheses[0].reason,
                        failure_type=rule_hypotheses[0].failure_type,
                        confidence=rule_hypotheses[0].rule_confidence
                        * (0.75 if console_truncated or console_partial else 1.0),
                        supporting_evidence=[list(valid_ids)[0]] if valid_ids else [],
                        contradicting_evidence=[],
                    )
                )

            final_evidence = Evidence(
                incident_id=event.incident_id,
                agent="jenkins_agent",
                created_at=datetime.now(timezone.utc),
                status="completed",
                failure_type=draft.failure_type if draft.failure_type != FailureTaxonomy.UNKNOWN and sanitized_hypotheses else (sanitized_hypotheses[0].failure_type if sanitized_hypotheses else FailureTaxonomy.UNKNOWN),
                summary=draft.summary,
                root_cause_hypotheses=sanitized_hypotheses,
                evidence_items=evidence_items,
                tool_calls=tool_calls,
                confidence=draft.confidence
                * (0.75 if console_truncated or console_partial else 1.0),
                recommended_next_steps=draft.recommended_next_steps or ["Inspect build logs"],
                failed_stage=parsed.failed_stage,
                failing_tests=[
                    redact(t.name)
                    for t in parsed.failing_tests[:MAX_TEST_FAILURES_IN_PROMPT]
                ],
                error_signature=parsed.error_signature,
                log_truncated=console_truncated,
                redaction_applied=True,
            )
        else:
            final_evidence = _fallback_evidence(
                event=event,
                parsed=parsed,
                hypotheses=rule_hypotheses,
                evidence_items=evidence_items,
                tool_calls=tool_calls,
                log_truncated=console_truncated,
                partial=console_partial,
            )

        # Post to Blackboard
        final_evidence = Evidence.model_validate(
            redact_value(final_evidence.model_dump(mode="json"))
        )
        await self.evidence_store.write(final_evidence)
        self.bb.post(
            BlackboardEntry(
                incident_id=event.incident_id,
                section="evidence",
                key="jenkins_agent",
                author="jenkins_agent",
                payload=final_evidence.model_dump(mode="json"),
            )
        )
        await self._announce(final_evidence)

        return final_evidence

    async def _announce(self, evidence: Evidence) -> None:
        """Notify the Coordinator after the evidence is persisted."""
        if self.notifier is None:
            return
        try:
            await self.notifier(evidence_ready(evidence.incident_id, evidence.agent))
        except Exception as error:
            logger.warning(
                "A2A evidence-ready notification failed (%s)",
                type(error).__name__,
            )


def run_investigation(incident_id: str | UUID, attempt: int = 0) -> Evidence:
    """Pure synchronous investigation runner called by A2A server and RabbitMQ."""
    bb = get_blackboard()
    inc_uuid = UUID(str(incident_id))

    # Read context entry if available
    context_entry = bb.read(inc_uuid, section="context", key="incident_event")
    if context_entry is None:
        raise ValueError(f"No incident context found on Blackboard for incident {inc_uuid}")
    payload = context_entry.payload
    required_fields = ("job_name", "build_number", "build_url", "branch", "git_commit", "timestamp")
    missing_fields = [field for field in required_fields if field not in payload]
    if missing_fields:
        raise ValueError(
            f"Incident context is missing required fields: {', '.join(missing_fields)}"
        )

    event = IncidentCreatedEvent(
        incident_id=inc_uuid,
        job_name=payload["job_name"],
        build_number=int(payload["build_number"]),
        build_url=payload["build_url"],
        branch=payload["branch"],
        git_commit=payload["git_commit"],
        failed_stage=payload.get("failed_stage"),
        timestamp=payload["timestamp"],
        remediation_attempt=int(payload.get("remediation_attempt", attempt)),
    )

    agent = JenkinsInvestigationAgent()

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, agent.run_event(event)).result()
    else:
        return asyncio.run(agent.run_event(event))

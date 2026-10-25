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
from agents.jenkins_agent.confidence import compute_confidence
from agents.jenkins_agent.config import JenkinsAgentSettings, load_settings
from agents.jenkins_agent.parsing import ParsedBuild, parse_build
from agents.jenkins_agent.history import find_related_incidents, RelatedIncidentResult
from agents.jenkins_agent.tools.flaky import (
    FLAKY_RECOMMENDATION,
    analyze_test_flakiness,
    create_flakiness_tool,
)
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
MAX_ATTEMPTS = int(os.getenv("REINVESTIGATION_MAX_ATTEMPTS", "5"))

# ---------------------------------------------------------------------------
# URL parsing helpers
# ---------------------------------------------------------------------------


def _parse_build_url(
    build_url: str, event: "IncidentCreatedEvent"
) -> "tuple[str, int]":
    """Extract (job_path, build_number) from a Jenkins build URL.

    Handles three URL shapes:

    - Standard multibranch: e.g. `.../job/target-app/job/main/42/`
    - View (PR/change-request): e.g. `.../job/target-app/view/change-requests/job/PR-7/5/`
    - Fallback: any other string -> returns event defaults

    Returns `(job_path, build_number)` where *job_path* uses `/` as separator.

    NOTE: To discover the exact trigger URL for your Jenkins instance, run::

        curl -u user:token JENKINS_URL/job/target-app/api/json?tree=jobs[name,url]

    and look for the `change-requests` view or multibranch branch list. Record the
    working URL in `docs/jenkins_pr_trigger.md` before wiring it up in production.
    """
    import re as _re

    url = build_url.rstrip("/")

    # Multibranch / folder path: collect all /job/<component> segments
    job_segments = _re.findall(r"/job/([^/]+)", url)
    if len(job_segments) >= 2:
        job_path = "/".join(job_segments)
        tail = _re.search(r"/(\d+)$", url)
        if tail:
            return job_path, int(tail.group(1))

    # View-style PR path: /job/<base>/view/<view>/job/<PR-n>/<number>
    pr_match = _re.search(
        r"/job/([^/]+)/view/[^/]+/job/([^/]+)/(\d+)$",
        url,
    )
    if pr_match:
        base_job = pr_match.group(1)
        pr_job = pr_match.group(2)
        build_num = int(pr_match.group(3))
        return f"{base_job}/{pr_job}", build_num

    logger.warning("_parse_build_url: cannot parse '%s'; using event defaults", build_url)
    return event.job_name, event.build_number


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
    history: list[dict[str, Any]] | None = None,
    flaky_score: float | None = None,
    related_result: RelatedIncidentResult | None = None,
) -> Evidence:
    """Build deterministic rule-based Evidence when LLM is unavailable or fails."""
    primary = hypotheses[0] if hypotheses else None
    failure_type = primary.failure_type if primary else FailureTaxonomy.UNKNOWN

    if flaky_score is not None and flaky_score > 0.4:
        failure_type = FailureTaxonomy.FLAKY_TEST
        recommended_next_steps = [FLAKY_RECOMMENDATION]
    else:
        recommended_next_steps = ["Inspect CI logs at point of failure"]

    boost = related_result.history_consistency_boost if related_result else 0.0
    breakdown = compute_confidence(
        parsed=parsed,
        rule_hypotheses=hypotheses,
        llm_hypotheses=None,
        log_meta={"truncated": log_truncated, "partial": partial},
        history=history or [],
        history_consistency_boost=boost,
    )
    confidence = breakdown.total

    source = redact(f"jenkins:{event.job_name}#{event.build_number}")
    items = list(evidence_items)
    if not any(item.id == "confidence_breakdown" for item in items):
        items.append(breakdown.as_evidence_item(source=source, timestamp=event.timestamp))

    valid_evidence_ids = {item.id for item in items}
    root_cause_hypotheses: list[EvidenceHypothesis] = []

    for hyp in hypotheses[:3]:
        matched = [eid for eid in hyp.matched_evidence_ids if eid in valid_evidence_ids]
        hyp_conf = breakdown.hypothesis_confidences.get(
            hyp.failure_type,
            hyp.rule_confidence * (0.8 if log_truncated or partial else 1.0),
        )
        root_cause_hypotheses.append(
            EvidenceHypothesis(
                hypothesis=hyp.reason,
                failure_type=hyp.failure_type,
                confidence=hyp_conf,
                supporting_evidence=matched,
                contradicting_evidence=[],
            )
        )

    if not root_cause_hypotheses:
        unknown_conf = breakdown.hypothesis_confidences.get(
            FailureTaxonomy.UNKNOWN,
            0.2 * (0.8 if log_truncated or partial else 1.0),
        )
        root_cause_hypotheses.append(
            EvidenceHypothesis(
                hypothesis="Insufficient evidence to determine exact failure cause",
                failure_type=FailureTaxonomy.UNKNOWN,
                confidence=unknown_conf,
                supporting_evidence=[item.id for item in items[:2]],
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
        evidence_items=items,
        tool_calls=tool_calls,
        confidence=confidence,
        recommended_next_steps=recommended_next_steps,
        failed_stage=parsed.failed_stage,
        failing_tests=[
            redact(t.name) for t in parsed.failing_tests[:MAX_TEST_FAILURES_IN_PROMPT]
        ],
        log_truncated=log_truncated,
        error_signature=parsed.error_signature,
        redaction_applied=True,
        flaky_score=flaky_score,
        related_incident_ids=related_result.related_incident_ids if related_result else [],
        recurrence_count=related_result.recurrence_count if related_result else 0,
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

        # Check flakiness for failing tests while the Jenkins client is still open
        flaky_score: float | None = None
        flaky_report = None
        if parsed.failing_tests:
            try:
                flaky_report = await analyze_test_flakiness(
                    job=event.job_name,
                    test_name=parsed.failing_tests[0].name,
                    client=client,
                )
                flaky_score = flaky_report.flaky_score
                tool_calls.append(
                    ToolCallRecord(tool="check_flakiness", duration_ms=10.0, ok=True)
                )
            except Exception as error:
                logger.debug("Flakiness check failed (%s)", error)
                tool_calls.append(
                    ToolCallRecord(tool="check_flakiness", duration_ms=10.0, ok=False)
                )

        # Rule-based classify with flaky_score
        rule_hypotheses = rule_based_classify(parsed, flaky_score=flaky_score)
        initial_failure_type = rule_hypotheses[0].failure_type.value if rule_hypotheses else None

        # Look up related incidents using error_signature (Prompt 5.2)
        related_result: RelatedIncidentResult | None = None
        if parsed.error_signature:
            try:
                related_result = await find_related_incidents(
                    error_signature=parsed.error_signature,
                    job_name=event.job_name,
                    current_incident_id=event.incident_id,
                    current_failure_type=initial_failure_type,
                    current_branch=event.branch,
                    bb=self.bb,
                    failed_stage=parsed.failed_stage or event.failed_stage,
                )
            except Exception as e:
                logger.warning("find_related_incidents failed (%s)", e)

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
        if flaky_report is not None:
            evidence_items.append(
                EvidenceItem(
                    id="flakiness",
                    kind="test_result",
                    source=redact(f"jenkins:{event.job_name}#{event.build_number}"),
                    timestamp=event.timestamp,
                    content=json.dumps(redact_value(flaky_report.to_dict())),
                )
            )

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

                flakiness_tool = create_flakiness_tool(event.job_name, client)
                tools = (
                    get_jenkins_mcp_tools(event.job_name, event.build_number)
                    + get_analytics_mcp_tools()
                    + [flakiness_tool]
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
            boost = related_result.history_consistency_boost if related_result else 0.0
            breakdown = compute_confidence(
                parsed=parsed,
                rule_hypotheses=rule_hypotheses,
                llm_hypotheses=draft.root_cause_hypotheses,
                log_meta={"truncated": console_truncated, "partial": console_partial},
                history=history,
                history_consistency_boost=boost,
            )

            source = redact(f"jenkins:{event.job_name}#{event.build_number}")
            items = list(evidence_items)
            if not any(item.id == "confidence_breakdown" for item in items):
                items.append(breakdown.as_evidence_item(source=source, timestamp=event.timestamp))

            # Ensure cited evidence IDs exist
            valid_ids = {item.id for item in items}
            sanitized_hypotheses: list[EvidenceHypothesis] = []
            for h in draft.root_cause_hypotheses:
                supporting = [eid for eid in h.supporting_evidence if eid in valid_ids]
                if not supporting and valid_ids:
                    supporting = [list(valid_ids)[0]]
                hyp_conf = breakdown.hypothesis_confidences.get(
                    h.failure_type,
                    h.confidence * (0.8 if console_truncated or console_partial else 1.0),
                )
                sanitized_hypotheses.append(
                    EvidenceHypothesis(
                        hypothesis=h.hypothesis,
                        failure_type=h.failure_type,
                        confidence=hyp_conf,
                        supporting_evidence=supporting,
                        contradicting_evidence=[],
                    )
                )

            if not sanitized_hypotheses and rule_hypotheses:
                rule_conf = breakdown.hypothesis_confidences.get(
                    rule_hypotheses[0].failure_type,
                    rule_hypotheses[0].rule_confidence * (0.8 if console_truncated or console_partial else 1.0),
                )
                sanitized_hypotheses.append(
                    EvidenceHypothesis(
                        hypothesis=rule_hypotheses[0].reason,
                        failure_type=rule_hypotheses[0].failure_type,
                        confidence=rule_conf,
                        supporting_evidence=[list(valid_ids)[0]] if valid_ids else [],
                        contradicting_evidence=[],
                    )
                )

            final_failure_type = draft.failure_type if draft.failure_type != FailureTaxonomy.UNKNOWN and sanitized_hypotheses else (sanitized_hypotheses[0].failure_type if sanitized_hypotheses else FailureTaxonomy.UNKNOWN)
            if flaky_score is not None and flaky_score > 0.4:
                final_failure_type = FailureTaxonomy.FLAKY_TEST
                final_recommended_steps = [FLAKY_RECOMMENDATION]
            else:
                final_recommended_steps = draft.recommended_next_steps or ["Inspect build logs"]

            final_evidence = Evidence(
                incident_id=event.incident_id,
                agent="jenkins_agent",
                created_at=datetime.now(timezone.utc),
                status="completed",
                failure_type=final_failure_type,
                summary=draft.summary,
                root_cause_hypotheses=sanitized_hypotheses,
                evidence_items=items,
                tool_calls=tool_calls,
                confidence=breakdown.total,
                recommended_next_steps=final_recommended_steps,
                failed_stage=parsed.failed_stage,
                failing_tests=[
                    redact(t.name)
                    for t in parsed.failing_tests[:MAX_TEST_FAILURES_IN_PROMPT]
                ],
                error_signature=parsed.error_signature,
                log_truncated=console_truncated,
                redaction_applied=True,
                flaky_score=flaky_score,
                related_incident_ids=related_result.related_incident_ids if related_result else [],
                recurrence_count=related_result.recurrence_count if related_result else 0,
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
                history=history,
                flaky_score=flaky_score,
                related_result=related_result,
            )

        close_client = getattr(client, "aclose", None)
        if close_client is not None:
            await close_client()

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


    async def run_reinvestigation(
        self,
        event: "IncidentCreatedEvent",
        *,
        attempt: int,
        previous_failure_summary: str | None,
        new_build_url: str | None,
    ) -> "Evidence":
        """Re-investigate the NEW validation build for an incident (attempt > 0).

        Decision tree:
        1. Build still running  → ``insufficient_evidence`` + advise to wait.
        2. Build passed + test-count check:
           a. Test count is equal or higher → ``completed`` with confidence 1.0 (close incident).
           b. Test count dropped → ``completed`` with reduced confidence + OPA flag suggestion.
        3. Build failed, same error_signature → fix didn't address the root cause.
        4. Build failed, new error_signature  → fix introduced or exposed a new problem.

        In cases 3 and 4 the CrewAI crew is re-run (LLM_OFFLINE exempted) with
        ``previous_attempts`` context injected into the task description so the LLM
        doesn't repeat the same hypothesis.

        The resulting Evidence is written as version ``attempt`` to the Blackboard.
        ``MAX_ATTEMPTS`` is enforced by the caller (``run_investigation``).
        """
        tool_calls: list["ToolCallRecord"] = []
        client = self.jenkins_client_factory()

        # ------------------------------------------------------------------
        # 1. Fetch the NEW build
        # ------------------------------------------------------------------
        new_job, new_num = _parse_build_url(new_build_url or event.build_url, event)
        source = redact(f"jenkins:{new_job}#{new_num}")

        try:
            build_info = await asyncio.wait_for(
                client.get_build(new_job, new_num),
                timeout=self.per_call_timeout,
            )
            tool_calls.append(ToolCallRecord(tool="get_build", duration_ms=10.0, ok=True))
        except Exception as exc:
            logger.warning("run_reinvestigation: get_build failed (%s)", type(exc).__name__)
            build_info = {}
            tool_calls.append(ToolCallRecord(tool="get_build", duration_ms=10.0, ok=False))

        build_result = str(build_info.get("result") or "").upper()
        is_building = bool(build_info.get("building"))

        # ------------------------------------------------------------------
        # 2. Build still running → defer
        # ------------------------------------------------------------------
        if is_building or (not build_result and not build_info):
            evidence = Evidence(
                incident_id=event.incident_id,
                agent="jenkins_agent",
                created_at=datetime.now(timezone.utc),
                status="insufficient_evidence",
                failure_type=FailureTaxonomy.UNKNOWN,
                summary=(
                    f"Validation build #{new_num} for attempt {attempt} is still running; "
                    "analysis deferred."
                ),
                evidence_items=[
                    EvidenceItem(
                        id="build_status",
                        kind="test_result",
                        source=source,
                        timestamp=datetime.now(timezone.utc),
                        content=json.dumps({"job": new_job, "build": new_num, "building": True}),
                    )
                ],
                tool_calls=tool_calls,
                confidence=0.0,
                recommended_next_steps=[
                    f"Wait for build #{new_num} to complete then re-trigger incident.reinvestigate",
                    "Consider increasing build_wait_timeout in JenkinsClient config",
                ],
                redaction_applied=True,
            )
            evidence = Evidence.model_validate(redact_value(evidence.model_dump(mode="json")))
            await self.evidence_store.write(evidence)
            self.bb.post(
                BlackboardEntry(
                    incident_id=event.incident_id,
                    section="evidence",
                    key="jenkins_agent",
                    author="jenkins_agent",
                    version=attempt + 1,
                    payload=evidence.model_dump(mode="json"),
                )
            )
            close_client = getattr(client, "aclose", None)
            if close_client is not None:
                await close_client()
            return evidence

        # ------------------------------------------------------------------
        # 3. Fetch test report + console for the NEW build
        # ------------------------------------------------------------------
        new_test_report: dict[str, Any] | None = None
        try:
            new_test_report = await asyncio.wait_for(
                client.get_test_report(new_job, new_num),
                timeout=self.per_call_timeout,
            )
        except Exception:
            pass

        console_text = ""
        console_truncated = False
        console_partial = False
        try:
            console_resp = await asyncio.wait_for(
                client.get_console_text(new_job, new_num),
                timeout=self.per_call_timeout,
            )
            console_text = console_resp.text
            console_truncated = console_resp.truncated
            console_partial = console_resp.partial
            tool_calls.append(ToolCallRecord(tool="get_console_text", duration_ms=10.0, ok=True))
        except Exception as exc:
            logger.warning("run_reinvestigation: console fetch failed (%s)", type(exc).__name__)
            tool_calls.append(ToolCallRecord(tool="get_console_text", duration_ms=10.0, ok=False))

        # ------------------------------------------------------------------
        # 4. Build PASSED → check test counts, record success or flag deletion
        # ------------------------------------------------------------------
        if build_result == "SUCCESS":
            new_total = (new_test_report or {}).get("totalCount", 0)
            # Read original evidence to compare test count
            original_entry = self.bb.read(event.incident_id, section="evidence", key="jenkins_agent")
            orig_total: int = 0
            if original_entry:
                orig_payload = original_entry.payload
                for item in orig_payload.get("evidence_items", []):
                    if item.get("id") == "test_report":
                        try:
                            orig_total = json.loads(item["content"]).get("totalCount", 0)
                        except Exception:
                            pass
                        break

            test_count_dropped = orig_total > 0 and new_total < orig_total
            if test_count_dropped:
                drop_amount = orig_total - new_total
                summary = (
                    f"WARNING: Validation build #{new_num} PASSED but test count dropped "
                    f"from {orig_total} to {new_total} ({drop_amount} tests removed). "
                    "A fix that deletes tests must not count as success."
                )
                confidence = max(0.0, 1.0 - (drop_amount / max(orig_total, 1)))
                recommended = [
                    f"Tell P3 to have OPA flag test deletions: {drop_amount} tests were removed",
                    "Verify removed tests were intentional and not masking failures",
                    f"Build #{new_num} passed but coverage may have regressed",
                ]
            else:
                summary = (
                    f"Validation build #{new_num} PASSED at attempt {attempt}. "
                    "Fix successfully addressed the root cause."
                )
                confidence = 1.0
                recommended = [
                    "Close incident — fix is validated",
                    f"Link PR that produced build #{new_num} as the resolution",
                ]

            evidence_items: list[EvidenceItem] = [
                EvidenceItem(
                    id="build_result",
                    kind="test_result",
                    source=source,
                    timestamp=datetime.now(timezone.utc),
                    content=json.dumps({
                        "job": new_job,
                        "build": new_num,
                        "result": "SUCCESS",
                        "original_test_count": orig_total,
                        "new_test_count": new_total,
                        "test_count_dropped": test_count_dropped,
                    }),
                )
            ]
            if new_test_report:
                evidence_items.append(
                    EvidenceItem(
                        id="test_report",
                        kind="test_result",
                        source=source,
                        timestamp=datetime.now(timezone.utc),
                        content=json.dumps(redact_value(new_test_report)),
                    )
                )

            evidence = Evidence(
                incident_id=event.incident_id,
                agent="jenkins_agent",
                created_at=datetime.now(timezone.utc),
                status="completed",
                failure_type=FailureTaxonomy.UNKNOWN,
                summary=summary,
                evidence_items=evidence_items,
                tool_calls=tool_calls,
                confidence=confidence,
                recommended_next_steps=recommended,
                redaction_applied=True,
            )
            evidence = Evidence.model_validate(redact_value(evidence.model_dump(mode="json")))
            await self.evidence_store.write(evidence)
            self.bb.post(
                BlackboardEntry(
                    incident_id=event.incident_id,
                    section="evidence",
                    key="jenkins_agent",
                    author="jenkins_agent",
                    version=attempt + 1,
                    payload=evidence.model_dump(mode="json"),
                )
            )
            close_client = getattr(client, "aclose", None)
            if close_client is not None:
                await close_client()
            return evidence

        # ------------------------------------------------------------------
        # 5. Build FAILED → parse new build, compare signatures
        # ------------------------------------------------------------------
        screened = screen_untrusted_text(console_text, source="console_log", incident_id=event.incident_id)
        stage_summary: dict[str, Any] = {}
        try:
            stage_summary = await asyncio.wait_for(
                client.get_stage_summary(new_job, new_num),
                timeout=self.per_call_timeout,
            )
        except Exception:
            pass

        new_parsed = parse_build(screened.text, stage_summary=stage_summary)

        # Flakiness re-check on the new build (edge case: flaky/env failure)
        flaky_score: float | None = None
        flaky_report = None
        if new_parsed.failing_tests:
            try:
                flaky_report = await analyze_test_flakiness(
                    job=new_job,
                    test_name=new_parsed.failing_tests[0].name,
                    client=client,
                )
                flaky_score = flaky_report.flaky_score
                tool_calls.append(ToolCallRecord(tool="check_flakiness", duration_ms=10.0, ok=True))
            except Exception as exc:
                logger.debug("Flakiness re-check failed (%s)", exc)
                tool_calls.append(ToolCallRecord(tool="check_flakiness", duration_ms=10.0, ok=False))

        # Read original error_signature from blackboard
        original_sig = ""
        original_hypotheses: list[dict[str, Any]] = []
        original_entry = self.bb.read(event.incident_id, section="evidence", key="jenkins_agent")
        if original_entry:
            original_sig = original_entry.payload.get("error_signature", "")
            original_hypotheses = original_entry.payload.get("root_cause_hypotheses", [])

        new_sig = new_parsed.error_signature
        if new_sig and original_sig and new_sig == original_sig:
            sig_comparison = "same_signature"
            sig_verdict = (
                "same_signature: the fix did not address the root cause — "
                "error signature is unchanged"
            )
        elif new_sig and original_sig:
            sig_comparison = "new_signature"
            sig_verdict = (
                "new_signature: the fix introduced or exposed a new problem — "
                "error signature has changed"
            )
        else:
            sig_comparison = "unknown"
            sig_verdict = "signature comparison inconclusive (one or both signatures missing)"

        # Previous-attempts context item (prevents repeating the same hypothesis)
        previous_attempts_item = EvidenceItem(
            id="previous_attempts",
            kind="log_excerpt",
            source=source,
            timestamp=datetime.now(timezone.utc),
            content=json.dumps({
                "attempt": attempt,
                "signature_comparison": sig_comparison,
                "original_error_signature": original_sig,
                "new_error_signature": new_sig,
                "previous_failure_summary": previous_failure_summary or "",
                "previous_hypotheses": [
                    {
                        "hypothesis": h.get("hypothesis", ""),
                        "failure_type": h.get("failure_type", ""),
                        "confidence": h.get("confidence", 0.0),
                    }
                    for h in original_hypotheses[:5]
                ],
            }),
        )

        # Build evidence items for this reinvestigation
        reinvest_evidence_items: list[EvidenceItem] = [previous_attempts_item]
        build_summary_content = {
            "job_name": redact(new_job),
            "build_number": new_num,
            "attempt": attempt,
            "result": build_result,
            "signature_comparison": sig_comparison,
            "console_truncated": console_truncated,
            "console_partial": console_partial,
        }
        reinvest_evidence_items.append(
            EvidenceItem(
                id="build_summary",
                kind="test_result",
                source=source,
                timestamp=datetime.now(timezone.utc),
                content=json.dumps(redact_value(build_summary_content)),
            )
        )
        if new_parsed.error_blocks:
            reinvest_evidence_items.append(
                EvidenceItem(
                    id="error_blocks",
                    kind="log_excerpt",
                    source=source,
                    timestamp=datetime.now(timezone.utc),
                    content=redact("\n---\n".join(new_parsed.error_blocks[:3])),
                )
            )
        if new_parsed.error_signature:
            reinvest_evidence_items.append(
                EvidenceItem(
                    id="error_signature",
                    kind="log_excerpt",
                    source=source,
                    timestamp=datetime.now(timezone.utc),
                    content=new_parsed.error_signature,
                )
            )
        if flaky_report is not None:
            reinvest_evidence_items.append(
                EvidenceItem(
                    id="flakiness",
                    kind="test_result",
                    source=source,
                    timestamp=datetime.now(timezone.utc),
                    content=json.dumps(redact_value(flaky_report.to_dict())),
                )
            )
        if new_test_report:
            reinvest_evidence_items.append(
                EvidenceItem(
                    id="test_report",
                    kind="test_result",
                    source=source,
                    timestamp=datetime.now(timezone.utc),
                    content=json.dumps(redact_value(new_test_report)),
                )
            )

        # ------------------------------------------------------------------
        # 6. Rule-based classify for the new failure
        # ------------------------------------------------------------------
        rule_hypotheses = rule_based_classify(new_parsed, flaky_score=flaky_score)
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
                flakiness_tool = create_flakiness_tool(new_job, client)
                tools = (
                    get_jenkins_mcp_tools(new_job, new_num)
                    + get_analytics_mcp_tools()
                    + [flakiness_tool]
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
                valid_eids = [item.id for item in reinvest_evidence_items]
                screened_job = screen_untrusted_text(new_job, source="jenkins_job_name").text
                screened_stage = screen_untrusted_text(
                    new_parsed.failed_stage or "unknown", source="jenkins_stage"
                ).text
                screened_sig = screen_untrusted_text(
                    new_parsed.error_signature or "unknown", source="jenkins_error_signature"
                ).text
                prev_hyp_summary = "; ".join(
                    h.get("hypothesis", "") for h in original_hypotheses[:3]
                )

                task_desc = (
                    f"RE-INVESTIGATION attempt {attempt} for Jenkins job {screened_job} "
                    f"build #{new_num}.\n"
                    f"Signature comparison: {sig_verdict}\n"
                    f"Failed Stage: {screened_stage}\n"
                    f"New Error Signature: {screened_sig}\n"
                    f"Previous failure summary: {previous_failure_summary or 'none'}\n"
                    f"Previous hypotheses (DO NOT REPEAT): {prev_hyp_summary}\n"
                    f"Console truncated: {console_truncated}; partial: {console_partial}.\n"
                    f"Rule Hypotheses: {[h.reason for h in rule_hypotheses]}\n"
                    f"Available Evidence IDs: {valid_eids}\n"
                    f"Ground every claim in a cited evidence ID. "
                    f"Do not repeat the previously-tried hypothesis. "
                    f"If the error signature is the same as before, explain why the fix "
                    f"was insufficient. If it is different, diagnose the new root cause."
                )
                task = Task(
                    description=task_desc,
                    expected_output="An EvidenceDraft structured object diagnosing the reinvestigation.",
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
                logger.warning("CrewAI reinvestigation fallback triggered (%s)", type(exc).__name__)
                draft = None

        # ------------------------------------------------------------------
        # 7. Build final reinvestigation Evidence
        # ------------------------------------------------------------------
        boost = 0.0
        breakdown = compute_confidence(
            parsed=new_parsed,
            rule_hypotheses=rule_hypotheses,
            llm_hypotheses=draft.root_cause_hypotheses if draft else None,
            log_meta={"truncated": console_truncated, "partial": console_partial},
            history=[],
            history_consistency_boost=boost,
        )

        valid_ids = {item.id for item in reinvest_evidence_items}
        reinvest_evidence_items.append(
            breakdown.as_evidence_item(source=source, timestamp=datetime.now(timezone.utc))
        )
        valid_ids.add("confidence_breakdown")

        if draft is not None:
            sanitized_hypotheses: list[EvidenceHypothesis] = []
            for h in draft.root_cause_hypotheses:
                supporting = [eid for eid in h.supporting_evidence if eid in valid_ids]
                if not supporting and valid_ids:
                    supporting = [list(valid_ids)[0]]
                hyp_conf = breakdown.hypothesis_confidences.get(h.failure_type, h.confidence)
                sanitized_hypotheses.append(
                    EvidenceHypothesis(
                        hypothesis=h.hypothesis,
                        failure_type=h.failure_type,
                        confidence=hyp_conf,
                        supporting_evidence=supporting,
                        contradicting_evidence=[],
                    )
                )
            if not sanitized_hypotheses and rule_hypotheses:
                sanitized_hypotheses.append(
                    EvidenceHypothesis(
                        hypothesis=rule_hypotheses[0].reason,
                        failure_type=rule_hypotheses[0].failure_type,
                        confidence=rule_hypotheses[0].rule_confidence,
                        supporting_evidence=[list(valid_ids)[0]] if valid_ids else [],
                        contradicting_evidence=[],
                    )
                )
            final_failure_type = (
                draft.failure_type
                if draft.failure_type != FailureTaxonomy.UNKNOWN and sanitized_hypotheses
                else (sanitized_hypotheses[0].failure_type if sanitized_hypotheses else FailureTaxonomy.UNKNOWN)
            )
            if flaky_score is not None and flaky_score > 0.4:
                final_failure_type = FailureTaxonomy.FLAKY_TEST
                recommended_steps = [FLAKY_RECOMMENDATION]
            else:
                recommended_steps = draft.recommended_next_steps or ["Inspect new build logs"]
            summary = f"{sig_verdict}. {draft.summary}"
        else:
            # Fallback: rule-based
            primary = rule_hypotheses[0] if rule_hypotheses else None
            final_failure_type = primary.failure_type if primary else FailureTaxonomy.UNKNOWN
            if flaky_score is not None and flaky_score > 0.4:
                final_failure_type = FailureTaxonomy.FLAKY_TEST
                recommended_steps = [FLAKY_RECOMMENDATION]
            else:
                recommended_steps = ["Inspect new build logs at point of failure"]
            sanitized_hypotheses = []
            if rule_hypotheses:
                sanitized_hypotheses.append(
                    EvidenceHypothesis(
                        hypothesis=rule_hypotheses[0].reason,
                        failure_type=rule_hypotheses[0].failure_type,
                        confidence=rule_hypotheses[0].rule_confidence,
                        supporting_evidence=[list(valid_ids)[0]] if valid_ids else [],
                        contradicting_evidence=[],
                    )
                )
            summary = f"{sig_verdict}."

        final_evidence = Evidence(
            incident_id=event.incident_id,
            agent="jenkins_agent",
            created_at=datetime.now(timezone.utc),
            status="completed",
            failure_type=final_failure_type,
            summary=summary,
            root_cause_hypotheses=sanitized_hypotheses,
            evidence_items=reinvest_evidence_items,
            tool_calls=tool_calls,
            confidence=breakdown.total,
            recommended_next_steps=recommended_steps,
            failed_stage=new_parsed.failed_stage,
            failing_tests=[redact(t.name) for t in new_parsed.failing_tests[:MAX_TEST_FAILURES_IN_PROMPT]],
            error_signature=new_parsed.error_signature,
            log_truncated=console_truncated,
            redaction_applied=True,
            flaky_score=flaky_score,
        )

        final_evidence = Evidence.model_validate(redact_value(final_evidence.model_dump(mode="json")))
        await self.evidence_store.write(final_evidence)
        self.bb.post(
            BlackboardEntry(
                incident_id=event.incident_id,
                section="evidence",
                key="jenkins_agent",
                author="jenkins_agent",
                version=attempt + 1,
                payload=final_evidence.model_dump(mode="json"),
            )
        )
        await self._announce(final_evidence)

        close_client = getattr(client, "aclose", None)
        if close_client is not None:
            await close_client()

        return final_evidence

def run_investigation(
    incident_id: str | UUID,
    attempt: int = 0,
    previous_failure_summary: str | None = None,
    new_build_url: str | None = None,
) -> Evidence:
    """Pure synchronous investigation runner called by A2A server and RabbitMQ.

    When ``attempt`` is 0 (first investigation), delegates to ``run_event``.
    When ``attempt > 0`` (reinvestigation after a fix attempt), delegates to
    ``run_reinvestigation`` with the new build URL and previous failure context.

    ``MAX_ATTEMPTS`` caps the total number of reinvestigation cycles to prevent
    runaway feedback loops that would exhaust free LLM quota.
    """
    if attempt >= MAX_ATTEMPTS:
        raise ValueError(
            f"MAX_ATTEMPTS ({MAX_ATTEMPTS}) reached for incident {incident_id}. "
            "Reinvestigation loop capped to prevent quota exhaustion. "
            "Set REINVESTIGATION_MAX_ATTEMPTS env var to increase the limit."
        )

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

    inv_agent = JenkinsInvestigationAgent()

    def _run_async(coro):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop and loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(asyncio.run, coro).result()
        else:
            return asyncio.run(coro)

    if attempt > 0 and new_build_url:
        # Reinvestigation: a fix was applied, validate the new build
        return _run_async(
            inv_agent.run_reinvestigation(
                event,
                attempt=attempt,
                previous_failure_summary=previous_failure_summary,
                new_build_url=new_build_url,
            )
        )
    else:
        return _run_async(inv_agent.run_event(event))

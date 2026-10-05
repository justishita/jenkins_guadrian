"""LangGraph Code & Remediation agent and its RabbitMQ worker.

**Owner:** P3. One of three agents that investigate an incident independently. It never
calls another agent: it reads GitHub, forms its own view of whether a recent change
explains the failure, and writes one evidence document to the shared store. The
Coordinator is what brings the three views together.

Week 2 delivers the retrieval and analysis half of that - context gathering, diff
analysis, and an end-to-end path from an incident event to a written evidence
document. The reasoning that turns signals into ranked hypotheses arrives in Week 3,
so until then this agent reports ``unknown`` with low confidence rather than guessing,
which is the behaviour TC-14 asks for anyway.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import time
from pathlib import Path
from typing import Any, Protocol, TypedDict

import aio_pika
from aio_pika.abc import AbstractIncomingMessage
from langgraph.graph import END, StateGraph
from pydantic import ValidationError

from agents.code_agent.config import CodeAgentSettings, load_settings
from agents.code_agent.diff_analysis import DiffAnalysis, FileRole, analyze_changes
from agents.code_agent.tools.github_client import (
    CommitSummary,
    FileChange,
    GitHubClient,
    GitHubError,
    PullRequestSummary,
)
from common.audit import AuditLog, AuditRecord, build_audit_log
from common.evidence_store import EvidenceStore, FileEvidenceStore
from common.models import (
    Evidence,
    EvidenceItem,
    EvidenceLocation,
    FailureTaxonomy,
    IncidentCreatedEvent,
    ToolCallRecord,
)
from common.redaction import redact


logger = logging.getLogger(__name__)

AGENT_NAME = "code_agent"

#: How many evidence items a single investigation may cite. Beyond this the document
#: stops being reviewable, and the Coordinator has to fuse three of them.
MAX_EVIDENCE_ITEMS = 40


class EvidenceWriter(Protocol):
    async def write(self, evidence: Evidence) -> None: ...


class CodeAgentState(TypedDict, total=False):
    """Everything one investigation accumulates, in the order the graph fills it."""

    event: IncidentCreatedEvent
    incident_id: str
    commit: CommitSummary | None
    recent_commits: list[CommitSummary]
    pull_requests: list[PullRequestSummary]
    changed_files: list[FileChange]
    analysis: DiffAnalysis
    tool_calls: list[ToolCallRecord]
    evidence_items: list[EvidenceItem]
    evidence: Evidence | None
    retrieval_failed: bool
    retrieval_error: str


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class CodeInvestigationAgent:
    """Investigates the code side of one incident and writes its evidence."""

    def __init__(
        self,
        github: GitHubClient,
        evidence_store: EvidenceWriter,
        audit_log: AuditLog,
        *,
        commit_lookback: int = 10,
    ) -> None:
        self._github = github
        self._evidence_store = evidence_store
        self._audit = audit_log
        self._commit_lookback = commit_lookback
        self._graph = self._build_graph()

    def _build_graph(self) -> Any:
        graph = StateGraph(CodeAgentState)
        graph.add_node("fetch_context", self.fetch_context)
        graph.add_node("analyze", self.analyze)
        graph.add_node("finalize", self.finalize)
        graph.set_entry_point("fetch_context")
        graph.add_edge("fetch_context", "analyze")
        graph.add_edge("analyze", "finalize")
        graph.add_edge("finalize", END)
        return graph.compile()

    # --- tool plumbing --------------------------------------------------------

    async def _timed_call(
        self, state: CodeAgentState, tool: str, args: dict[str, Any], call: Any
    ) -> Any:
        """Run one GitHub call, recording its duration and outcome either way.

        A failed tool call is evidence about the investigation, so it is recorded
        rather than dropped; the caller decides whether it is fatal.
        """
        started = time.monotonic()
        ok = True
        try:
            return await call
        except GitHubError:
            ok = False
            raise
        finally:
            duration = (time.monotonic() - started) * 1000
            state.setdefault("tool_calls", []).append(
                ToolCallRecord(tool=tool, args=args, duration_ms=duration, ok=ok)
            )
            await self._audit.record(
                AuditRecord(
                    incident_id=state["event"].incident_id,
                    actor=AGENT_NAME,
                    event_type="tool_call",
                    summary=tool,
                    payload={"args": args},
                    duration_ms=duration,
                    ok=ok,
                )
            )

    # --- graph nodes ----------------------------------------------------------

    async def fetch_context(self, state: CodeAgentState) -> dict[str, Any]:
        """Retrieve the failing commit, its pull requests, and recent branch history.

        Partial retrieval is normal and useful: a commit that GitHub cannot show us
        still leaves the branch history to reason over. Only a total failure to learn
        anything is reported as a retrieval failure.
        """
        event = state["event"]
        commit: CommitSummary | None = None
        changed_files: list[FileChange] = []
        pull_requests: list[PullRequestSummary] = []
        recent_commits: list[CommitSummary] = []
        errors: list[str] = []

        try:
            commit, changed_files = await self._timed_call(
                state,
                "get_commit",
                {"sha": event.git_commit},
                self._github.get_commit(event.git_commit),
            )
        except GitHubError as error:
            errors.append(f"get_commit: {error}")

        try:
            pull_requests = await self._timed_call(
                state,
                "list_pull_requests_for_commit",
                {"sha": event.git_commit},
                self._github.list_pull_requests_for_commit(event.git_commit),
            )
        except GitHubError as error:
            errors.append(f"list_pull_requests_for_commit: {error}")

        try:
            recent_commits = await self._timed_call(
                state,
                "list_recent_commits",
                {"branch": event.branch, "limit": self._commit_lookback},
                self._github.list_recent_commits(event.branch, self._commit_lookback),
            )
        except GitHubError as error:
            errors.append(f"list_recent_commits: {error}")

        retrieval_failed = commit is None and not recent_commits
        if errors:
            logger.warning(
                "github retrieval was incomplete",
                extra={"incident_id": state["incident_id"], "errors": errors},
            )

        return {
            "commit": commit,
            "changed_files": changed_files,
            "pull_requests": pull_requests,
            "recent_commits": recent_commits,
            "retrieval_failed": retrieval_failed,
            "retrieval_error": redact("; ".join(errors)),
        }

    async def analyze(self, state: CodeAgentState) -> dict[str, Any]:
        """Classify the change set and turn it into citable evidence items."""
        analysis = analyze_changes(state.get("changed_files") or [])
        return {"analysis": analysis, "evidence_items": build_evidence_items(state, analysis)}

    async def finalize(self, state: CodeAgentState) -> dict[str, Any]:
        """Build the evidence document and persist it to the shared store."""
        evidence = build_evidence(state)
        await self._evidence_store.write(evidence)
        await self._audit.record(
            AuditRecord(
                incident_id=evidence.incident_id,
                actor=AGENT_NAME,
                event_type="evidence_written",
                summary=evidence.summary,
                payload={
                    "status": evidence.status,
                    "failure_type": evidence.failure_type.value,
                    "confidence": evidence.confidence,
                    "evidence_items": len(evidence.evidence_items),
                },
                ok=True,
            )
        )
        return {"evidence": evidence}

    # --- entry point ----------------------------------------------------------

    async def run_event(self, event: IncidentCreatedEvent) -> Evidence:
        """Investigate one incident end to end and return the evidence written.

        A failure anywhere still produces an evidence document: the Coordinator must
        be able to see that this agent ran and found nothing, which is different from
        the agent never having reported at all.
        """
        state: CodeAgentState = {
            "event": event,
            "incident_id": str(event.incident_id),
            "tool_calls": [],
        }
        async with self._audit.agent_run(event.incident_id, AGENT_NAME) as details:
            try:
                final: CodeAgentState = await self._graph.ainvoke(state)  # type: ignore[assignment]
            except Exception as error:
                logger.exception(
                    "code investigation failed", extra={"incident_id": str(event.incident_id)}
                )
                evidence = failed_evidence(state, error)
                await self._evidence_store.write(evidence)
                details.update({"status": "failed", "error": type(error).__name__})
                return evidence

            evidence = final.get("evidence")
            if evidence is None:  # pragma: no cover - the graph always finalizes
                evidence = failed_evidence(final, RuntimeError("graph produced no evidence"))
                await self._evidence_store.write(evidence)
            details.update(
                {
                    "status": evidence.status,
                    "failure_type": evidence.failure_type.value,
                    "confidence": evidence.confidence,
                    "tool_calls": len(final.get("tool_calls") or []),
                }
            )
            return evidence


# --- evidence construction ----------------------------------------------------


def build_evidence_items(state: CodeAgentState, analysis: DiffAnalysis) -> list[EvidenceItem]:
    """Build the citable items: commits, pull requests and classified file changes.

    Every claim the agent later makes must point at one of these IDs, which is what
    stops a hypothesis from being unfalsifiable prose.
    """
    items: list[EvidenceItem] = []

    commit = state.get("commit")
    if commit is not None:
        items.append(
            EvidenceItem(
                id="commit-head",
                kind="commit",
                source=commit.url or "github",
                timestamp=_parse_timestamp(commit.authored_at),
                content=f"{commit.short_sha} by {commit.author}: {commit.subject}",
            )
        )

    for pull_request in state.get("pull_requests") or []:
        items.append(
            EvidenceItem(
                id=f"pr-{pull_request.number}",
                kind="commit",
                source=pull_request.url or "github",
                content=(
                    f"PR #{pull_request.number} ({pull_request.state}"
                    f"{', merged' if pull_request.merged else ''}) by {pull_request.author}: "
                    f"{pull_request.title}"
                ),
            )
        )

    for index, file in enumerate(analysis.files, start=1):
        role = analysis.roles.get(file.path, FileRole.OTHER)
        items.append(
            EvidenceItem(
                id=f"file-{index}",
                kind="commit",
                source=file.path,
                content=(
                    f"{file.status} {role.value}: +{file.additions}/-{file.deletions} lines"
                ),
                location=EvidenceLocation(file=file.path),
            )
        )

    for index, change in enumerate(analysis.dependency_changes, start=1):
        items.append(
            EvidenceItem(
                id=f"dependency-{index}",
                kind="commit",
                source=change.path,
                content=change.describe(),
                location=EvidenceLocation(file=change.path),
            )
        )

    for index, change in enumerate(analysis.configuration_changes, start=1):
        items.append(
            EvidenceItem(
                id=f"config-{index}",
                kind="commit",
                source=change.path,
                content=change.describe(),
                location=EvidenceLocation(file=change.path),
            )
        )

    for commit in (state.get("recent_commits") or [])[:5]:
        head = state.get("commit")
        if head is not None and commit.sha == head.sha:
            continue
        items.append(
            EvidenceItem(
                id=f"history-{commit.short_sha}",
                kind="commit",
                source=commit.url or "github",
                timestamp=_parse_timestamp(commit.authored_at),
                content=f"{commit.short_sha} by {commit.author}: {commit.subject}",
            )
        )

    return items[:MAX_EVIDENCE_ITEMS]


def build_evidence(state: CodeAgentState) -> Evidence:
    """Assemble the evidence document for a completed investigation.

    Week 2 reports what it found without classifying it. The ``unknown`` type and low
    confidence are deliberate: the Week 3 reasoning step replaces them, and until it
    exists the agent must not let the Coordinator weigh a guess.
    """
    event = state["event"]
    analysis: DiffAnalysis = state.get("analysis") or analyze_changes([])
    items = state.get("evidence_items") or []

    if state.get("retrieval_failed"):
        status = "failed"
        summary = f"Could not retrieve code context from GitHub: {state.get('retrieval_error', '')}".strip()
        next_steps = [
            "Check GITHUB_TOKEN scope and GITHUB_REPO, then re-run the code investigation."
        ]
    elif analysis.is_empty:
        status = "insufficient_evidence"
        summary = (
            f"No file changes found for commit {event.git_commit[:7]} on {event.branch}; "
            "the failure is unlikely to have a code cause."
        )
        next_steps = ["Correlate with the Jenkins and metrics evidence for this incident."]
    else:
        status = "completed"
        summary = (
            f"Commit {event.git_commit[:7]} on {event.branch} changed {analysis.summarize()}."
        )
        next_steps = ["Review the cited file changes against the failing build stage."]

    return Evidence(
        incident_id=event.incident_id,
        agent=AGENT_NAME,
        created_at=_utc_now(),
        status=status,  # type: ignore[arg-type]
        failure_type=FailureTaxonomy.UNKNOWN,
        summary=redact(summary),
        root_cause_hypotheses=[],
        evidence_items=items,
        tool_calls=state.get("tool_calls") or [],
        # Retrieval and classification are not a root cause. Confidence stays low
        # until the Week 3 reasoning step earns it.
        confidence=0.2 if status == "completed" else 0.1,
        recommended_next_steps=next_steps,
        failed_stage=event.failed_stage,
        redaction_applied=True,
    )


def failed_evidence(state: CodeAgentState, error: BaseException) -> Evidence:
    """Report a crashed investigation as evidence rather than as silence."""
    event = state["event"]
    return Evidence(
        incident_id=event.incident_id,
        agent=AGENT_NAME,
        created_at=_utc_now(),
        status="failed",
        failure_type=FailureTaxonomy.UNKNOWN,
        summary=redact(f"Code investigation failed: {type(error).__name__}: {error}"),
        evidence_items=[],
        tool_calls=state.get("tool_calls") or [],
        confidence=0.0,
        recommended_next_steps=["Re-run the code investigation for this incident."],
        failed_stage=event.failed_stage,
        redaction_applied=True,
    )


def _parse_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# --- worker -------------------------------------------------------------------


def parse_event(body: bytes) -> IncidentCreatedEvent:
    """Validate a raw message body; raises ValueError/ValidationError on bad input."""
    return IncidentCreatedEvent.model_validate(json.loads(body))


def build_agent(settings: CodeAgentSettings) -> tuple[CodeInvestigationAgent, GitHubClient]:
    """Wire the agent from settings, returning the client so the caller can close it."""
    github = GitHubClient(
        settings.GITHUB_REPO,
        settings.GITHUB_TOKEN,
        api_url=settings.GITHUB_API_URL,
        timeout=settings.GITHUB_TIMEOUT_SECONDS,
        max_retries=settings.GITHUB_MAX_RETRIES,
        backoff_seconds=settings.GITHUB_BACKOFF_SECONDS,
    )
    agent = CodeInvestigationAgent(
        github,
        FileEvidenceStore(Path(settings.EVIDENCE_DIR)),
        build_audit_log(database_url=settings.DATABASE_URL or None, root=Path(settings.AUDIT_DIR)),
        commit_lookback=settings.COMMIT_LOOKBACK,
    )
    return agent, github


async def _process_message(message: AbstractIncomingMessage, agent: CodeInvestigationAgent) -> None:
    # requeue=False: a malformed message must not loop forever.
    async with message.process(requeue=False):
        try:
            event = parse_event(message.body)
        except (ValueError, ValidationError):
            logger.exception("dropping malformed incident message")
            return
        try:
            await agent.run_event(event)
        except Exception:  # the consumer must outlive any single bad incident
            logger.exception(
                "code investigation failed", extra={"incident_id": str(event.incident_id)}
            )


async def _connect_with_retry(
    url: str, retries: int, backoff_seconds: float
) -> aio_pika.abc.AbstractRobustConnection:
    """Connect to RabbitMQ, retrying with bounded exponential backoff."""
    for attempt in range(retries + 1):
        try:
            return await aio_pika.connect_robust(url)
        except (OSError, aio_pika.exceptions.AMQPConnectionError) as error:
            if attempt == retries:
                logger.error("rabbitmq unreachable, giving up", extra={"attempts": attempt + 1})
                raise
            delay = min(backoff_seconds * (2**attempt), 30.0)
            logger.warning(
                "rabbitmq connection failed, retrying",
                extra={"attempt": attempt + 1, "retry_in_seconds": delay, "error": str(error)},
            )
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


async def run(settings: CodeAgentSettings) -> None:
    """Consume incident events and investigate each one."""
    agent, github = build_agent(settings)
    try:
        connection = await _connect_with_retry(
            settings.RABBITMQ_URL,
            settings.RABBITMQ_CONNECT_RETRIES,
            settings.RABBITMQ_CONNECT_BACKOFF_SECONDS,
        )
        async with connection:
            channel = await connection.channel()
            await channel.set_qos(prefetch_count=1)
            exchange = await channel.declare_exchange(
                settings.EXCHANGE_NAME, aio_pika.ExchangeType.TOPIC, durable=True
            )
            queue = await channel.declare_queue(settings.QUEUE_NAME, durable=True)
            await queue.bind(exchange, routing_key=settings.ROUTING_KEY)
            logger.info("code agent consuming", extra={"queue": settings.QUEUE_NAME})
            async with queue.iterator() as messages:
                async for message in messages:
                    await _process_message(message, agent)
    finally:
        await github.aclose()


def main() -> None:
    settings = load_settings()
    logging.basicConfig(
        level=settings.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    asyncio.run(run(settings))


if __name__ == "__main__":
    main()

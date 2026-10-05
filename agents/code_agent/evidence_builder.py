"""Assemble the citable evidence items for one code investigation.

**Owner:** P3. Split out from the agent because two things need the same IDs: the
evidence document, and the hypotheses that cite it. Generating the scheme twice is how
a hypothesis ends up pointing at an evidence ID that does not exist - which the shared
``Evidence`` model rejects outright, so the drift would surface as a crash on a real
incident rather than as a wrong answer. ``EvidenceIndex`` owns the scheme once and
hands out the IDs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from agents.code_agent.diff_analysis import (
    ConfigurationChange,
    DependencyChange,
    DiffAnalysis,
    FileRole,
)
from agents.code_agent.tools.github_client import CommitSummary, PullRequestSummary
from common.models import EvidenceItem, EvidenceLocation


#: How many items one investigation may cite. Beyond this the document stops being
#: reviewable, and the Coordinator still has to fuse three of them.
MAX_EVIDENCE_ITEMS = 40

#: Only the few most recent commits are worth citing as history; older ones are
#: context, not evidence.
MAX_HISTORY_ITEMS = 5

HEAD_COMMIT_ID = "commit-head"


def parse_timestamp(value: str) -> datetime | None:
    """Parse a GitHub ISO-8601 timestamp into an aware UTC datetime, or ``None``."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass
class EvidenceIndex:
    """The evidence items for one investigation, plus the IDs to cite them by."""

    items: list[EvidenceItem] = field(default_factory=list)
    _dependency_ids: dict[str, str] = field(default_factory=dict)
    _configuration_ids: dict[str, str] = field(default_factory=dict)
    _file_ids: dict[str, str] = field(default_factory=dict)
    _has_head_commit: bool = False

    @property
    def head_commit_id(self) -> str | None:
        """The failing commit's ID, or ``None`` when GitHub could not show it."""
        return HEAD_COMMIT_ID if self._has_head_commit else None

    def dependency_id(self, change: DependencyChange) -> str | None:
        return self._dependency_ids.get(f"{change.path}::{change.name}")

    def configuration_id(self, change: ConfigurationChange) -> str | None:
        return self._configuration_ids.get(f"{change.path}::{change.key}")

    def file_id(self, path: str) -> str | None:
        return self._file_ids.get(path)

    def file_ids_for(self, analysis: DiffAnalysis, role: FileRole) -> tuple[str, ...]:
        """IDs of every cited file with the given role."""
        return tuple(
            identifier
            for path in analysis.paths_with_role(role)
            if (identifier := self._file_ids.get(path)) is not None
        )

    def known(self, *identifiers: str | None) -> tuple[str, ...]:
        """Keep only IDs that are actually present, preserving order.

        A hypothesis may only cite evidence that exists; filtering here is what lets
        the reasoning rules name their supports without checking each one.
        """
        present = {item.id for item in self.items}
        seen: set[str] = set()
        result: list[str] = []
        for identifier in identifiers:
            if identifier and identifier in present and identifier not in seen:
                seen.add(identifier)
                result.append(identifier)
        return tuple(result)


def build_evidence_index(
    analysis: DiffAnalysis,
    *,
    commit: CommitSummary | None = None,
    pull_requests: tuple[PullRequestSummary, ...] | list[PullRequestSummary] = (),
    recent_commits: tuple[CommitSummary, ...] | list[CommitSummary] = (),
) -> EvidenceIndex:
    """Build every citable item for an investigation and index it by what it describes."""
    index = EvidenceIndex()

    if commit is not None:
        index.items.append(
            EvidenceItem(
                id=HEAD_COMMIT_ID,
                kind="commit",
                source=commit.url or "github",
                timestamp=parse_timestamp(commit.authored_at),
                content=f"{commit.short_sha} by {commit.author}: {commit.subject}",
            )
        )
        index._has_head_commit = True

    for pull_request in pull_requests:
        index.items.append(
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

    for position, file in enumerate(analysis.files, start=1):
        identifier = f"file-{position}"
        role = analysis.roles.get(file.path, FileRole.OTHER)
        index.items.append(
            EvidenceItem(
                id=identifier,
                kind="commit",
                source=file.path,
                content=f"{file.status} {role.value}: +{file.additions}/-{file.deletions} lines",
                location=EvidenceLocation(file=file.path),
            )
        )
        index._file_ids[file.path] = identifier

    for position, change in enumerate(analysis.dependency_changes, start=1):
        identifier = f"dependency-{position}"
        index.items.append(
            EvidenceItem(
                id=identifier,
                kind="commit",
                source=change.path,
                content=change.describe(),
                location=EvidenceLocation(file=change.path),
            )
        )
        index._dependency_ids[f"{change.path}::{change.name}"] = identifier

    for position, change in enumerate(analysis.configuration_changes, start=1):
        identifier = f"config-{position}"
        index.items.append(
            EvidenceItem(
                id=identifier,
                kind="commit",
                source=change.path,
                content=change.describe(),
                location=EvidenceLocation(file=change.path),
            )
        )
        index._configuration_ids[f"{change.path}::{change.key}"] = identifier

    head_sha = commit.sha if commit is not None else None
    for historic in list(recent_commits)[:MAX_HISTORY_ITEMS]:
        if historic.sha == head_sha:
            continue
        index.items.append(
            EvidenceItem(
                id=f"history-{historic.short_sha}",
                kind="commit",
                source=historic.url or "github",
                timestamp=parse_timestamp(historic.authored_at),
                content=f"{historic.short_sha} by {historic.author}: {historic.subject}",
            )
        )

    # Trim from the tail: history is the least load-bearing, and the entries above it
    # are the ones hypotheses cite.
    del index.items[MAX_EVIDENCE_ITEMS:]
    return index


__all__ = [
    "HEAD_COMMIT_ID",
    "MAX_EVIDENCE_ITEMS",
    "MAX_HISTORY_ITEMS",
    "EvidenceIndex",
    "build_evidence_index",
    "parse_timestamp",
]

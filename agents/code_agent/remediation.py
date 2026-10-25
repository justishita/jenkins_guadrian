"""Week 5: turn an actionable incident report into a minimal, structured fix plan.

**Owner:** P3. The remediation engine proposes; it never applies. Its output is a
``FixPlan`` that the OPA policy gate checks, a human approves, and only then becomes a
draft pull request (Week 6).

The fix is built as **the inverse of exactly the lines that broke the build**. A
dependency pin that moved from 0.28.1 to 0.99.0 is moved back; a configuration value
changed from ``3`` to ``-5`` is set back to ``3``. Each edit is the removed line from
the breaking diff replacing the added one, verbatim, so formatting, comments and
neighbouring lines are untouched. Nothing is rewritten, reformatted or "improved".

It proposes only what it can justify, and says so when it cannot:

* the incident report must be actionable - a resolved cause at or above the floor;
* only ``dependency_regression`` and ``config_error`` have automated fixes, because
  only those have a provable previous good state in the diff itself;
* a configuration fix reverts only values that are checkably wrong, never every
  changed key - reverting a legitimate change is its own incident;
* an added dependency or a removed key has no line to restore, so it is left to a
  human rather than guessed at.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agents.code_agent.diff_analysis import (
    ConfigurationChange,
    DiffLine,
    FileRole,
    _parse_configuration,
    _parse_dependency,
    classify_path,
    iter_diff_lines,
)
from agents.code_agent.reasoning import suspicious_configuration_reasons
from agents.code_agent.tools.github_client import FileChange
from agents.code_agent.tools.github_writer import GitHubWriter
from common.fix_branch import format_fix_branch, parse_fix_branch
from common.models import FailureTaxonomy
from common.redaction import redact
from coordinator.synthesis import IncidentReport


#: Failure types with an automated fix. Anything else is reported, never patched.
SUPPORTED_FAILURE_TYPES = frozenset(
    {FailureTaxonomy.DEPENDENCY_REGRESSION, FailureTaxonomy.CONFIG_ERROR}
)


class EditNotApplicable(ValueError):
    """The target file no longer contains the line the edit expects, exactly once."""


class FileEdit(BaseModel):
    """Replace one exact line with another. The smallest change there is."""

    model_config = ConfigDict(frozen=True)

    path: str = Field(min_length=1)
    before: str
    after: str
    reason: str = ""

    @property
    def patch(self) -> str:
        """A unified-diff hunk for this edit, as the policy gate and a reviewer read it."""
        return f"@@ -1,1 +1,1 @@\n-{self.before}\n+{self.after}"

    def apply(self, content: str) -> str:
        """Apply the edit to a file's current text.

        Refuses unless ``before`` appears as a whole line **exactly once**. Zero means
        the file moved on since the incident and the fix is stale; more than one means
        the edit is ambiguous. Either way, guessing which line to change is how an
        automated fix breaks something new.
        """
        lines = content.splitlines(keepends=True)
        matches = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == self.before]
        if len(matches) != 1:
            raise EditNotApplicable(
                f"{self.path}: expected the line {self.before!r} exactly once, found {len(matches)}"
            )
        index = matches[0]
        line = lines[index]
        ending = line[len(line.rstrip("\r\n")) :]
        lines[index] = self.after + ending
        return "".join(lines)


class FixPlan(BaseModel):
    """A proposed minimal fix, with everything a reviewer needs to judge it."""

    model_config = ConfigDict(frozen=True)

    incident_id: UUID
    failure_type: FailureTaxonomy
    confidence: float = Field(ge=0, le=1)
    attempt: int = Field(default=0, ge=0)
    base_ref: str = Field(min_length=1)
    branch_name: str = Field(min_length=1)
    commit_message: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    edits: tuple[FileEdit, ...] = Field(min_length=1)
    verification: tuple[str, ...] = ()

    @model_validator(mode="after")
    def branch_links_to_this_plan(self) -> "FixPlan":
        """Refuse a branch that would not link back to this incident and attempt.

        Validation runs are matched to incidents by branch name alone, so a plan with
        any other name would open a PR whose re-validation could never be linked.
        """
        parsed = parse_fix_branch(self.branch_name)
        if parsed != (self.incident_id, self.attempt):
            raise ValueError(
                f"branch_name must be {format_fix_branch(self.incident_id, self.attempt)!r}, "
                f"got {self.branch_name!r}"
            )
        return self

    @property
    def changed_paths(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(edit.path for edit in self.edits))

    def changed_files(self) -> list[dict[str, object]]:
        """Per-file summary in the shape ``policies/remediation.rego`` expects."""
        files: list[dict[str, object]] = []
        for path in self.changed_paths:
            edits = [edit for edit in self.edits if edit.path == path]
            files.append(
                {
                    "path": path,
                    "status": "modified",
                    "additions": len(edits),
                    "deletions": len(edits),
                    "patch": "\n".join(edit.patch for edit in edits),
                }
            )
        return files

    def to_policy_input(self) -> dict[str, object]:
        """The document handed to OPA's ``jenkinsguardians.remediation`` package."""
        return {
            "incident_id": str(self.incident_id),
            "failure_type": self.failure_type.value,
            "confidence": self.confidence,
            "attempt": self.attempt,
            "changed_files": self.changed_files(),
        }

    def to_markdown(self) -> str:
        """The proposal as a reviewer sees it, and the body of the draft PR."""
        lines = [
            f"## Proposed fix for incident {self.incident_id}",
            "",
            f"**Cause:** {self.failure_type.value} at {self.confidence:.2f} confidence  ",
            f"**Attempt:** {self.attempt}  ",
            f"**Branch:** `{self.branch_name}` from `{self.base_ref[:12]}`",
            "",
            self.rationale,
            "",
            "### Changes",
            "",
        ]
        for edit in self.edits:
            lines += [f"`{edit.path}`", "", "```diff", f"-{edit.before}", f"+{edit.after}", "```", ""]
            if edit.reason:
                lines += [f"_{edit.reason}_", ""]
        if self.verification:
            lines += ["### How this is verified", ""]
            lines += [f"1. {step}" for step in self.verification]
        return "\n".join(lines)


class NoFix(BaseModel):
    """Why no fix was proposed. As much a result as a plan is."""

    model_config = ConfigDict(frozen=True)

    incident_id: UUID
    reason: str = Field(min_length=1)


Proposal = FixPlan | NoFix


def _pairs(
    files: Sequence[FileChange], roles: frozenset[FileRole], parse: object
) -> list[tuple[str, str, DiffLine, DiffLine]]:
    """Pair each removed line with the added line for the same key, per file.

    Returns ``(path, key, removed, added)`` for every key whose value actually changed.
    A key only removed or only added has no pair and is skipped - there is nothing on
    the other side to restore.
    """
    parser = parse  # a (line) -> (key, value) | None callable
    pairs: list[tuple[str, str, DiffLine, DiffLine]] = []
    for file in files:
        if classify_path(file.path) not in roles:
            continue
        removed: dict[str, DiffLine] = {}
        added: dict[str, DiffLine] = {}
        values: dict[tuple[str, bool], str] = {}
        for line in iter_diff_lines(file):
            parsed = parser(line.content)  # type: ignore[operator]
            if parsed is None:
                continue
            key, value = parsed
            (added if line.added else removed)[key] = line
            values[(key, line.added)] = value
        for key in sorted(set(removed) & set(added)):
            if values[(key, False)] != values[(key, True)]:
                pairs.append((file.path, key, removed[key], added[key]))
    return pairs


def _dependency_edits(files: Sequence[FileChange]) -> list[FileEdit]:
    return [
        FileEdit(
            path=path,
            before=added.content,
            after=removed.content,
            reason=f"Restore {name} to the version from the last good build.",
        )
        for path, name, removed, added in _pairs(
            files, frozenset({FileRole.DEPENDENCY_MANIFEST}), _parse_dependency
        )
    ]


def _configuration_edits(files: Sequence[FileChange]) -> list[FileEdit]:
    edits: list[FileEdit] = []
    for path, key, removed, added in _pairs(
        files, frozenset({FileRole.CONFIGURATION, FileRole.DEPLOYMENT}), _parse_configuration
    ):
        previous = _parse_configuration(removed.content)
        current = _parse_configuration(added.content)
        assert previous is not None and current is not None  # guaranteed by _pairs
        reasons = suspicious_configuration_reasons(
            ConfigurationChange(key=key, path=path, previous_value=previous[1], new_value=current[1])
        )
        if not reasons:
            # A plausible-looking change is not reverted: it may be the intended one.
            continue
        edits.append(
            FileEdit(
                path=path,
                before=added.content,
                after=removed.content,
                reason=f"Restore {key}: {'; '.join(reasons)}.",
            )
        )
    return edits


def branch_name_for(incident_id: UUID, attempt: int) -> str:
    """``ai-fix/<incident_id>-<attempt>`` - parseable back to both, so re-validation
    runs link to their incident automatically (``common.fix_branch``)."""
    return format_fix_branch(incident_id, attempt)


def propose_fix(
    report: IncidentReport,
    files: Sequence[FileChange],
    *,
    base_ref: str,
    attempt: int = 0,
) -> Proposal:
    """Build a minimal fix plan for an incident, or explain why there is none.

    ``files`` are the changed files from the commit the report blames, as the code
    agent's GitHub client returned them. ``base_ref`` is the commit the fix branches
    from. Never raises for "cannot fix" - that is a ``NoFix`` with its reason.
    """
    incident_id = report.incident_id

    if not report.actionable:
        return NoFix(
            incident_id=incident_id,
            reason=(
                f"The incident report is not actionable (status {report.status}, "
                f"{report.failure_type.value} at {report.confidence:.2f}); "
                "no fix is proposed from unresolved or low-confidence evidence."
            ),
        )

    if report.failure_type not in SUPPORTED_FAILURE_TYPES:
        return NoFix(
            incident_id=incident_id,
            reason=(
                f"{report.failure_type.value} has no automated fix. Only dependency "
                "regressions and configuration errors carry a provable previous good "
                "state in the diff; this one needs a human."
            ),
        )

    if report.failure_type is FailureTaxonomy.DEPENDENCY_REGRESSION:
        edits = _dependency_edits(files)
        if not edits:
            return NoFix(
                incident_id=incident_id,
                reason="No dependency pin moved in this commit, so there is no previous version to restore.",
            )
        names = {parsed[0] for edit in edits if (parsed := _parse_dependency(edit.after))}
        what = ", ".join(sorted(names))
        title = f"Pin {what} back to the last good version"
    else:
        edits = _configuration_edits(files)
        if not edits:
            return NoFix(
                incident_id=incident_id,
                reason=(
                    "No changed configuration value is checkably wrong. Reverting a "
                    "plausible change could undo the intended one; a human should choose."
                ),
            )
        title = f"Restore {len(edits)} configuration value{'s' if len(edits) != 1 else ''}"

    short = str(incident_id)[:8]
    rationale = redact(
        f"{report.summary} The fix reverts only the {len(edits)} line"
        f"{'s' if len(edits) != 1 else ''} that changed in the breaking commit, "
        "back to their previous values. Nothing else is modified."
    )
    return FixPlan(
        incident_id=incident_id,
        failure_type=report.failure_type,
        confidence=report.confidence,
        attempt=attempt,
        base_ref=base_ref,
        branch_name=branch_name_for(incident_id, attempt),
        commit_message=f"{title} (incident {short})\n\n{rationale}",
        rationale=rationale,
        edits=tuple(edits),
        verification=(
            "Jenkins re-runs the full pipeline on the draft branch.",
            "The incident is resolved only if every stage passes; a failure re-opens investigation.",
        ),
    )


@dataclass(frozen=True, slots=True)
class BranchResult:
    """What the branching workflow did, for the audit trail and the pull request."""

    branch: str
    base_ref: str
    commits: tuple[str, ...]


async def apply_plan_to_branch(plan: FixPlan, writer: GitHubWriter) -> BranchResult:
    """Create the remediation branch and commit the plan's edits to it.

    Each file is read **from the new branch** - i.e. exactly as it was at the commit
    that broke the build - and every edit is applied against that text with
    ``FileEdit.apply``, which refuses unless the line it replaces appears exactly
    once. The commit is conditioned on the blob sha that was read, so if anything
    touched the file in between, GitHub refuses rather than overwriting it.

    Nothing here merges, approves or opens a pull request. It only prepares a branch
    a human can look at.
    """
    await writer.create_branch(plan.branch_name, plan.base_ref)
    commits: list[str] = []
    for path in plan.changed_paths:
        current = await writer.read_file(path, plan.branch_name)
        content = current.content
        for edit in plan.edits:
            if edit.path == path:
                content = edit.apply(content)
        commits.append(
            await writer.commit_file(
                path,
                content,
                branch=plan.branch_name,
                message=plan.commit_message,
                blob_sha=current.sha,
            )
        )
    return BranchResult(branch=plan.branch_name, base_ref=plan.base_ref, commits=tuple(commits))


__all__ = [
    "SUPPORTED_FAILURE_TYPES",
    "BranchResult",
    "apply_plan_to_branch",
    "EditNotApplicable",
    "FileEdit",
    "FixPlan",
    "NoFix",
    "Proposal",
    "branch_name_for",
    "propose_fix",
]


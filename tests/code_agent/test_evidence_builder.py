"""Tests for evidence assembly and the citation index.

**Owner:** P3. The index exists so that the evidence document and the hypotheses that
cite it cannot disagree about an ID. These tests pin that invariant down, because the
shared ``Evidence`` model rejects a dangling citation outright - drift here would
surface as a crash on a real incident, not as a wrong answer in a test.
"""

from __future__ import annotations

import pytest

from agents.code_agent.diff_analysis import FileRole, analyze_changes
from agents.code_agent.evidence_builder import (
	HEAD_COMMIT_ID,
	MAX_EVIDENCE_ITEMS,
	build_evidence_index,
	parse_timestamp,
)
from agents.code_agent.tools.github_client import CommitSummary, FileChange, PullRequestSummary


def commit(sha: str = "a1b2c3d4e5f6", subject: str = "Upgrade httpx") -> CommitSummary:
	return CommitSummary(
		sha=sha, message=subject, author="Ishita", authored_at="2026-10-05T09:30:00Z",
		url=f"https://github.com/o/r/commit/{sha}",
	)


def change(path: str, patch: str = "") -> FileChange:
	return FileChange(path=path, status="modified", additions=1, deletions=1, patch=patch)


DEPENDENCY = change("target_app/requirements.txt", "@@ -4,1 +4,1 @@\n-httpx==0.28.1\n+httpx==0.99.0")
CONFIG = change(
	"target_app/config/settings.yaml",
	"@@ -2,1 +2,1 @@\n-database_timeout_seconds: 3\n+database_timeout_seconds: -5",
)


def test_the_head_commit_is_cited_when_it_was_retrieved() -> None:
	index = build_evidence_index(analyze_changes([]), commit=commit())

	assert index.head_commit_id == HEAD_COMMIT_ID
	assert any(item.id == HEAD_COMMIT_ID for item in index.items)


def test_there_is_no_head_commit_id_when_github_could_not_show_it() -> None:
	"""A hypothesis must not cite a commit the agent never retrieved."""
	index = build_evidence_index(analyze_changes([DEPENDENCY]))

	assert index.head_commit_id is None
	assert index.known(index.head_commit_id) == ()


def test_each_signal_is_addressable_by_what_it_describes() -> None:
	analysis = analyze_changes([DEPENDENCY, CONFIG])
	index = build_evidence_index(analysis, commit=commit())

	dependency = analysis.dependency_changes[0]
	configuration = analysis.configuration_changes[0]

	assert index.dependency_id(dependency) == "dependency-1"
	assert index.configuration_id(configuration) == "config-1"
	assert index.file_id("target_app/requirements.txt") == "file-1"
	assert index.file_ids_for(analysis, FileRole.DEPENDENCY_MANIFEST) == ("file-1",)


def test_an_unknown_lookup_returns_none_rather_than_a_fabricated_id() -> None:
	index = build_evidence_index(analyze_changes([DEPENDENCY]))

	assert index.file_id("target_app/app/main.py") is None


def test_known_filters_out_ids_that_do_not_exist() -> None:
	index = build_evidence_index(analyze_changes([DEPENDENCY]), commit=commit())

	assert index.known("dependency-1", "dependency-99", None, "file-1") == ("dependency-1", "file-1")


def test_known_removes_duplicates_while_keeping_order() -> None:
	index = build_evidence_index(analyze_changes([DEPENDENCY]), commit=commit())

	assert index.known("file-1", HEAD_COMMIT_ID, "file-1") == ("file-1", HEAD_COMMIT_ID)


def test_pull_requests_are_cited_by_number() -> None:
	pull_request = PullRequestSummary(
		number=42, title="Upgrade httpx", state="closed", merged=True,
		author="ishita", url="https://github.com/o/r/pull/42",
	)
	index = build_evidence_index(analyze_changes([]), pull_requests=[pull_request])

	item = next(item for item in index.items if item.id == "pr-42")
	assert "merged" in item.content
	assert "ishita" in item.content


def test_the_head_commit_is_not_repeated_in_the_history() -> None:
	head = commit("aaaaaaa")
	index = build_evidence_index(
		analyze_changes([]), commit=head, recent_commits=[head, commit("bbbbbbb")]
	)
	ids = [item.id for item in index.items]

	assert ids.count(HEAD_COMMIT_ID) == 1
	assert "history-bbbbbbb" in ids
	assert "history-aaaaaaa" not in ids


def test_citable_items_are_capped() -> None:
	files = [change(f"target_app/app/m{i}.py") for i in range(60)]
	index = build_evidence_index(analyze_changes(files), commit=commit())

	assert len(index.items) == MAX_EVIDENCE_ITEMS


def test_the_cap_keeps_the_items_hypotheses_cite() -> None:
	"""History is trimmed first: it is the least load-bearing evidence."""
	files = [change(f"target_app/app/m{i}.py") for i in range(45)]
	history = [commit(f"hist{i:03d}") for i in range(5)]
	index = build_evidence_index(analyze_changes(files), commit=commit(), recent_commits=history)
	ids = [item.id for item in index.items]

	assert HEAD_COMMIT_ID in ids
	assert not any(identifier.startswith("history-") for identifier in ids)


def test_file_evidence_records_its_role_and_location() -> None:
	analysis = analyze_changes([CONFIG])
	index = build_evidence_index(analysis)
	item = next(item for item in index.items if item.id == "file-1")

	assert "configuration" in item.content
	assert item.location is not None
	assert item.location.file == "target_app/config/settings.yaml"


@pytest.mark.parametrize(
	("value", "expected_iso"),
	[
		("2026-10-05T09:30:00Z", "2026-10-05T09:30:00+00:00"),
		("2026-10-05T09:30:00+00:00", "2026-10-05T09:30:00+00:00"),
	],
)
def test_github_timestamps_become_aware_utc(value: str, expected_iso: str) -> None:
	parsed = parse_timestamp(value)

	assert parsed is not None
	assert parsed.isoformat() == expected_iso


@pytest.mark.parametrize("value", ["", "not a date", "2026-13-45"])
def test_an_unparseable_timestamp_is_none_rather_than_an_error(value: str) -> None:
	assert parse_timestamp(value) is None

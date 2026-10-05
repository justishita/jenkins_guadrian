"""Tests for the diff-analysis layer.

**Owner:** P3. These are the signals the Week 3 reasoning is built on, so they are
tested without a network or an LLM in sight. The cases that matter most are the ones
where a naive reading would produce a confident wrong answer: diff headers that look
like content, comments that look like assignments, and removed lines that look added.
"""

from __future__ import annotations

import pytest

from agents.code_agent.diff_analysis import (
	FileRole,
	analyze_changes,
	classify_path,
	extract_configuration_changes,
	extract_dependency_changes,
	iter_diff_lines,
)
from agents.code_agent.tools.github_client import FileChange


def change(path: str, patch: str = "", *, status: str = "modified", additions: int = 1, deletions: int = 1) -> FileChange:
	return FileChange(
		path=path, status=status, additions=additions, deletions=deletions, patch=patch
	)


@pytest.mark.parametrize(
	("path", "expected"),
	[
		("target_app/requirements.txt", FileRole.DEPENDENCY_MANIFEST),
		("pyproject.toml", FileRole.DEPENDENCY_MANIFEST),
		("package-lock.json", FileRole.DEPENDENCY_MANIFEST),
		("target_app/config/settings.yaml", FileRole.CONFIGURATION),
		("target_app/config/deploy.env", FileRole.CONFIGURATION),
		("target_app/Jenkinsfile", FileRole.CI_PIPELINE),
		(".github/workflows/ci.yml", FileRole.CI_PIPELINE),
		("target_app/deploy/deploy.sh", FileRole.DEPLOYMENT),
		("target_app/Dockerfile", FileRole.DEPLOYMENT),
		("target_app/tests/test_orders.py", FileRole.TEST),
		("tests/code_agent/helpers.py", FileRole.TEST),
		("target_app/app/main.py", FileRole.SOURCE),
		("docs/decisions.md", FileRole.DOCUMENTATION),
		("assets/logo.png", FileRole.OTHER),
		("", FileRole.OTHER),
	],
)
def test_paths_are_classified_by_what_the_file_is_for(path: str, expected: FileRole) -> None:
	assert classify_path(path) is expected


def test_a_ci_definition_outranks_its_yaml_suffix() -> None:
	"""`.github/workflows/x.yml` is a pipeline, not application configuration."""
	assert classify_path(".github/workflows/build.yml") is FileRole.CI_PIPELINE


def test_a_test_file_outranks_its_python_suffix() -> None:
	assert classify_path("target_app/tests/test_orders.py") is FileRole.TEST


def test_diff_headers_are_not_mistaken_for_changed_lines() -> None:
	patch = (
		"diff --git a/x.py b/x.py\n"
		"index 111..222 100644\n"
		"--- a/x.py\n"
		"+++ b/x.py\n"
		"@@ -1,2 +1,2 @@\n"
		"-old = 1\n"
		"+new = 2\n"
	)
	lines = list(iter_diff_lines(change("target_app/app/x.py", patch)))

	assert [(line.added, line.content) for line in lines] == [(False, "old = 1"), (True, "new = 2")]


def test_an_empty_patch_yields_no_lines() -> None:
	assert list(iter_diff_lines(change("docs/diagram.png", ""))) == []


# --- dependency signals -------------------------------------------------------


def test_a_moved_pin_is_reported_as_a_version_change() -> None:
	patch = "@@ -4,1 +4,1 @@\n-httpx==0.28.1\n+httpx==0.99.0"
	changes = extract_dependency_changes([change("target_app/requirements.txt", patch)])

	assert len(changes) == 1
	assert changes[0].name == "httpx"
	assert changes[0].previous_version == "==0.28.1"
	assert changes[0].new_version == "==0.99.0"
	assert changes[0].is_upgrade_or_downgrade is True
	assert "httpx ==0.28.1 -> ==0.99.0" in changes[0].describe()


def test_an_added_dependency_has_no_previous_version() -> None:
	changes = extract_dependency_changes(
		[change("target_app/requirements.txt", "@@ -0,0 +1,1 @@\n+requests==2.32.0")]
	)

	assert changes[0].previous_version is None
	assert changes[0].new_version == "==2.32.0"
	assert changes[0].is_upgrade_or_downgrade is False
	assert "added" in changes[0].describe()


def test_a_removed_dependency_has_no_new_version() -> None:
	changes = extract_dependency_changes(
		[change("target_app/requirements.txt", "@@ -1,1 +0,0 @@\n-requests==2.32.0")]
	)

	assert changes[0].new_version is None
	assert "removed" in changes[0].describe()


def test_an_unchanged_pin_in_a_moved_block_is_not_reported() -> None:
	"""Reordering a manifest shows the same pin removed and added - that is not a change."""
	patch = "@@ -1,3 +1,3 @@\n-httpx==0.28.1\n-fastapi==0.115.12\n+fastapi==0.115.12\n+httpx==0.28.1"
	assert extract_dependency_changes([change("target_app/requirements.txt", patch)]) == ()


def test_comments_in_a_manifest_are_ignored() -> None:
	patch = "@@ -1,1 +1,2 @@\n+# pinned for the security advisory\n+httpx==0.28.1"
	changes = extract_dependency_changes([change("target_app/requirements.txt", patch)])

	assert [c.name for c in changes] == ["httpx"]


def test_json_manifest_dependencies_are_parsed() -> None:
	patch = '@@ -5,1 +5,1 @@\n-    "axios": "^1.6.0"\n+    "axios": "^1.7.9"'
	changes = extract_dependency_changes([change("package.json", patch)])

	assert changes[0].name == "axios"
	assert changes[0].previous_version == "^1.6.0"


def test_dependency_extraction_ignores_non_manifest_files() -> None:
	patch = "@@ -1,1 +1,1 @@\n-httpx==0.28.1\n+httpx==0.99.0"
	assert extract_dependency_changes([change("target_app/app/main.py", patch)]) == ()


# --- configuration signals ----------------------------------------------------


def test_a_changed_configuration_value_is_reported_with_both_values() -> None:
	patch = "@@ -2,1 +2,1 @@\n-database_timeout_seconds: 3\n+database_timeout_seconds: -5"
	changes = extract_configuration_changes([change("target_app/config/settings.yaml", patch)])

	assert len(changes) == 1
	assert changes[0].key == "database_timeout_seconds"
	assert changes[0].previous_value == "3"
	assert changes[0].new_value == "-5"
	assert "'3' -> '-5'" in changes[0].describe()


def test_an_env_file_assignment_is_parsed() -> None:
	patch = "@@ -2,1 +2,1 @@\n-DATABASE_URL=postgresql://db/app\n+DATABASE_URL=mysql://db/app"
	changes = extract_configuration_changes([change("target_app/config/deploy.env", patch)])

	assert changes[0].key == "DATABASE_URL"
	assert changes[0].new_value == "mysql://db/app"


def test_a_removed_configuration_key_is_reported() -> None:
	patch = "@@ -3,1 +0,0 @@\n-enable_slow: false"
	changes = extract_configuration_changes([change("target_app/config/settings.yaml", patch)])

	assert changes[0].new_value is None
	assert "removed" in changes[0].describe()


def test_trailing_comments_are_stripped_from_configuration_values() -> None:
	patch = "@@ -2,1 +2,1 @@\n-database_timeout_seconds: 3\n+database_timeout_seconds: 30  # was too low"
	changes = extract_configuration_changes([change("target_app/config/settings.yaml", patch)])

	assert changes[0].new_value == "30"


def test_configuration_extraction_ignores_source_files() -> None:
	patch = "@@ -1,1 +1,1 @@\n-timeout = 3\n+timeout = 30"
	assert extract_configuration_changes([change("target_app/app/main.py", patch)]) == ()


# --- whole-changeset analysis -------------------------------------------------


def test_analysis_of_an_empty_change_set_says_so() -> None:
	analysis = analyze_changes([])

	assert analysis.is_empty is True
	assert analysis.total_changed_lines == 0
	assert analysis.summarize() == "no files changed"


def test_analysis_collects_roles_counts_and_signals() -> None:
	files = [
		change(
			"target_app/requirements.txt",
			"@@ -4,1 +4,1 @@\n-httpx==0.28.1\n+httpx==0.99.0",
			additions=1,
			deletions=1,
		),
		change(
			"target_app/config/settings.yaml",
			"@@ -2,1 +2,1 @@\n-database_timeout_seconds: 3\n+database_timeout_seconds: -5",
			additions=1,
			deletions=1,
		),
	]
	analysis = analyze_changes(files)

	assert analysis.total_changed_lines == 4
	assert analysis.paths_with_role(FileRole.DEPENDENCY_MANIFEST) == ("target_app/requirements.txt",)
	assert len(analysis.dependency_changes) == 1
	assert len(analysis.configuration_changes) == 1
	assert len(analysis.added_lines) == 2
	assert len(analysis.removed_lines) == 2
	assert "2 file(s), 4 line(s)" in analysis.summarize()


def test_a_documentation_only_change_is_recognisable_as_such() -> None:
	analysis = analyze_changes([change("README.md", "@@ -1,1 +1,1 @@\n-old\n+new")])

	assert analysis.touches_only_documentation is True


def test_a_mixed_change_is_not_documentation_only() -> None:
	analysis = analyze_changes(
		[change("README.md"), change("target_app/app/main.py", "@@ -1,1 +1,1 @@\n+x = 1")]
	)

	assert analysis.touches_only_documentation is False


def test_an_empty_change_set_is_not_documentation_only() -> None:
	"""`all()` over nothing is true; "no changes" must not read as "docs only"."""
	assert analyze_changes([]).touches_only_documentation is False

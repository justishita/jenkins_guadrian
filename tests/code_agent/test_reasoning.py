"""Tests for the code-side root-cause reasoning.

**Owner:** P3. The scenario suite checks the headline cases end to end; this checks the
rules that decide them. The ones worth the most attention are the restraints - the
confidence floor, the cap on ``unknown``, and the reduction when a competing
explanation exists - because those are what stop a confident wrong answer.
"""

from __future__ import annotations

import pytest

from agents.code_agent.diff_analysis import ConfigurationChange, analyze_changes
from agents.code_agent.evidence_builder import build_evidence_index
from agents.code_agent.reasoning import (
	MAX_UNKNOWN_CONFIDENCE,
	MIN_REPORTABLE_CONFIDENCE,
	IncidentContext,
	reason,
	suspicious_configuration_reasons,
)
from agents.code_agent.tools.github_client import FileChange
from common.models import FailureTaxonomy


def change(path: str, patch: str = "", *, additions: int = 1, deletions: int = 1, status: str = "modified") -> FileChange:
	return FileChange(path=path, status=status, additions=additions, deletions=deletions, patch=patch)


DEPENDENCY_BUMP = change(
	"target_app/requirements.txt", "@@ -4,1 +4,1 @@\n-httpx==0.28.1\n+httpx==0.99.0"
)
CONFIG_BREAK = change(
	"target_app/config/settings.yaml",
	"@@ -2,1 +2,1 @@\n-database_timeout_seconds: 3\n+database_timeout_seconds: -5",
)
SOURCE_EDIT = change("target_app/app/main.py", "@@ -1,1 +1,1 @@\n-a = 1\n+a = 2")


def run(files: list[FileChange], *, failed_stage: str | None = "Test"):
	analysis = analyze_changes(files)
	index = build_evidence_index(analysis)
	context = IncidentContext(failed_stage=failed_stage, branch="main", git_commit="a1b2c3d")
	return reason(analysis, context, index)


# --- TC-03: dependency regression ---------------------------------------------


def test_a_moved_pin_is_a_dependency_regression() -> None:
	result = run([DEPENDENCY_BUMP])

	assert result.failure_type is FailureTaxonomy.DEPENDENCY_REGRESSION
	assert result.status == "completed"
	assert result.confidence >= 0.8
	assert "httpx" in result.summary


def test_a_dependency_hypothesis_cites_the_dependency_change(tmp_path: object) -> None:
	analysis = analyze_changes([DEPENDENCY_BUMP])
	index = build_evidence_index(analysis)
	result = reason(analysis, IncidentContext(failed_stage="Test"), index)

	assert "dependency-1" in result.hypotheses[0].supporting_evidence


def test_a_source_change_alongside_a_bump_lowers_confidence_and_is_cited_against() -> None:
	"""A competing explanation is a real reduction, not a footnote."""
	alone = run([DEPENDENCY_BUMP])
	together = run([DEPENDENCY_BUMP, SOURCE_EDIT])

	dependency = next(
		h for h in together.hypotheses if h.failure_type is FailureTaxonomy.DEPENDENCY_REGRESSION
	)
	assert dependency.confidence < alone.confidence
	assert dependency.contradicting_evidence  # the source files argue against it


def test_an_added_dependency_is_weaker_evidence_than_a_moved_pin() -> None:
	added = change("target_app/requirements.txt", "@@ -0,0 +1,1 @@\n+requests==2.32.0")

	assert run([added]).confidence < run([DEPENDENCY_BUMP]).confidence


def test_a_stage_unrelated_to_dependencies_lowers_confidence() -> None:
	assert run([DEPENDENCY_BUMP], failed_stage="Deploy").confidence < run(
		[DEPENDENCY_BUMP], failed_stage="Build"
	).confidence


def test_the_next_steps_never_suggest_hiding_the_failure() -> None:
	steps = " ".join(run([DEPENDENCY_BUMP]).recommended_next_steps).lower()

	assert "pin" in steps
	assert "skip" not in steps
	assert "disable" not in steps


# --- TC-08: configuration error -----------------------------------------------


def test_a_broken_configuration_value_is_a_config_error() -> None:
	result = run([CONFIG_BREAK])

	assert result.failure_type is FailureTaxonomy.CONFIG_ERROR
	assert result.confidence >= 0.8
	assert "database_timeout_seconds" in result.summary


def test_a_plausible_configuration_change_scores_lower_than_a_broken_one() -> None:
	plausible = change(
		"target_app/config/settings.yaml",
		"@@ -2,1 +2,1 @@\n-database_timeout_seconds: 3\n+database_timeout_seconds: 5",
	)

	assert run([plausible]).confidence < run([CONFIG_BREAK]).confidence


@pytest.mark.parametrize(
	("key", "previous", "new", "expected_fragment"),
	[
		("database_timeout_seconds", "3", "-5", "not a positive value"),
		("database_timeout_seconds", "3", "0", "not a positive value"),
		("ORDERS_PORT", "8010", "0", "not a positive value"),
		("database_timeout_seconds", "3", "soon", "changed type"),
		("enable_slow", "false", "maybe", "changed type"),
		("DATABASE_URL", "postgresql://db/app", "", "emptied"),
		("DATABASE_URL", "postgresql://db/app", "mysql://db/app", "scheme changed"),
		("DATABASE_URL", "postgresql://db/app", "not-a-url", "no longer a URL"),
	],
)
def test_broken_values_are_recognised_with_a_quotable_reason(
	key: str, previous: str, new: str, expected_fragment: str
) -> None:
	reasons = suspicious_configuration_reasons(
		ConfigurationChange(key=key, path="target_app/config/settings.yaml", previous_value=previous, new_value=new)
	)

	assert any(expected_fragment in reason for reason in reasons), reasons


@pytest.mark.parametrize(
	("key", "previous", "new"),
	[
		("database_timeout_seconds", "3", "30"),
		("enable_slow", "false", "true"),
		("DATABASE_URL", "postgresql://a/b", "postgresql://c/d"),
		("log_level", "INFO", "DEBUG"),
	],
)
def test_a_reasonable_value_change_is_not_flagged(key: str, previous: str, new: str) -> None:
	assert (
		suspicious_configuration_reasons(
			ConfigurationChange(key=key, path="target_app/config/settings.yaml", previous_value=previous, new_value=new)
		)
		== ()
	)


def test_a_removed_key_is_flagged() -> None:
	reasons = suspicious_configuration_reasons(
		ConfigurationChange(key="database_url", path="x.yaml", previous_value="postgresql://a/b", new_value=None)
	)

	assert "was removed" in reasons[0]


# --- restraint ----------------------------------------------------------------


def test_no_changed_files_is_reported_as_no_code_cause() -> None:
	"""TC-12: the agent must not blame the most recent commit it can see."""
	result = run([])

	assert result.failure_type is FailureTaxonomy.UNKNOWN
	assert result.status == "insufficient_evidence"
	assert "does not explain this failure" in result.summary


def test_a_documentation_only_change_is_not_blamed() -> None:
	result = run([change("README.md", "@@ -1,1 +1,1 @@\n-old\n+new")])

	assert result.failure_type is FailureTaxonomy.UNKNOWN
	assert "cannot affect the build" in result.hypotheses[0].hypothesis


def test_unknown_is_never_asserted_confidently() -> None:
	for result in (run([]), run([change("README.md", "@@ -1,1 +1,1 @@\n+x")])):
		for hypothesis in result.hypotheses:
			if hypothesis.failure_type is FailureTaxonomy.UNKNOWN:
				assert hypothesis.confidence <= MAX_UNKNOWN_CONFIDENCE


def test_a_weak_best_hypothesis_is_reported_as_insufficient_evidence() -> None:
	"""TC-14: below the floor the agent reports what it saw, not what it suspects."""
	wide = [change(f"target_app/app/m{i}.py", "@@ -1,1 +1,1 @@\n-a=1\n+a=2") for i in range(6)]
	result = run(wide, failed_stage=None)

	assert result.confidence < MIN_REPORTABLE_CONFIDENCE
	assert result.status == "insufficient_evidence"
	assert result.failure_type is FailureTaxonomy.UNKNOWN
	# The hypotheses are still reported - only the conclusion is withheld.
	assert result.hypotheses


def test_the_floor_matches_the_policy_minimum() -> None:
	"""The agent must not propose fixes from evidence OPA would refuse to act on."""
	rego = (
		__import__("pathlib").Path(__file__).resolve().parents[2] / "policies" / "remediation.rego"
	).read_text(encoding="utf-8")

	assert f"min_confidence := {MIN_REPORTABLE_CONFIDENCE}" in rego


# --- ranking ------------------------------------------------------------------


def test_hypotheses_are_ranked_by_confidence() -> None:
	result = run([DEPENDENCY_BUMP, CONFIG_BREAK, SOURCE_EDIT])
	confidences = [hypothesis.confidence for hypothesis in result.hypotheses]

	assert confidences == sorted(confidences, reverse=True)
	assert result.failure_type is result.hypotheses[0].failure_type


def test_competing_signals_produce_several_hypotheses_not_one_certainty() -> None:
	"""TC-13: with multiple signals, the strongest wins but the others are recorded."""
	result = run([DEPENDENCY_BUMP, CONFIG_BREAK])

	kinds = {hypothesis.failure_type for hypothesis in result.hypotheses}
	assert FailureTaxonomy.DEPENDENCY_REGRESSION in kinds
	assert FailureTaxonomy.CONFIG_ERROR in kinds


def test_a_build_stage_failure_in_source_is_a_compilation_hypothesis() -> None:
	result = run([SOURCE_EDIT], failed_stage="Build")

	assert result.failure_type is FailureTaxonomy.BUILD_COMPILATION_FAILURE


def test_a_test_stage_failure_in_source_is_a_test_failure_hypothesis() -> None:
	result = run([SOURCE_EDIT], failed_stage="Test")

	assert result.failure_type is FailureTaxonomy.CODE_TEST_FAILURE


def test_a_deployment_change_at_the_deploy_stage_is_a_deployment_failure() -> None:
	result = run([change("target_app/deploy/deploy.sh", "@@ -1,1 +1,1 @@\n-a\n+b")], failed_stage="Deploy")

	assert result.failure_type is FailureTaxonomy.DEPLOYMENT_FAILURE


def test_every_hypothesis_cites_at_least_one_piece_of_evidence() -> None:
	analysis = analyze_changes([DEPENDENCY_BUMP, CONFIG_BREAK, SOURCE_EDIT])
	index = build_evidence_index(analysis)
	result = reason(analysis, IncidentContext(failed_stage="Test"), index)

	known = {item.id for item in index.items}
	for hypothesis in result.hypotheses:
		assert hypothesis.supporting_evidence
		assert set(hypothesis.supporting_evidence) <= known
		assert set(hypothesis.contradicting_evidence) <= known

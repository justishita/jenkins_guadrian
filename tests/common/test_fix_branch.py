"""``ai-fix/<incident_id>-<attempt>`` must round-trip, or validation cannot be linked."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from common.fix_branch import format_fix_branch, is_fix_branch, parse_fix_branch


INCIDENT = UUID("11111111-2222-3333-4444-555555555555")


@pytest.mark.parametrize("attempt", [0, 1, 7, 42])
def test_format_and_parse_round_trip(attempt: int) -> None:
	name = format_fix_branch(INCIDENT, attempt)

	assert name == f"ai-fix/{INCIDENT}-{attempt}"
	assert parse_fix_branch(name) == (INCIDENT, attempt)


def test_round_trip_for_random_incidents() -> None:
	for _ in range(50):
		incident = uuid4()
		assert parse_fix_branch(format_fix_branch(incident, 3)) == (incident, 3)


def test_a_string_incident_id_is_normalised() -> None:
	assert format_fix_branch(str(INCIDENT).upper(), 0) == f"ai-fix/{INCIDENT}-0"


@pytest.mark.parametrize("prefix", ["refs/heads/", "origin/"])
def test_ci_style_ref_prefixes_are_accepted(prefix: str) -> None:
	assert parse_fix_branch(f"{prefix}ai-fix/{INCIDENT}-2") == (INCIDENT, 2)


@pytest.mark.parametrize(
	"name",
	[
		"main",
		f"remediation/{INCIDENT}-0",
		f"ai-fix/{INCIDENT}",
		f"ai-fix/{INCIDENT}-",
		f"ai-fix/{INCIDENT}--1",
		f"ai-fix/{INCIDENT}-01",
		f"ai-fix/{INCIDENT}-1-extra",
		"ai-fix/11111111-2222-3333-4444-55555555555-0",
		"ai-fix/ABCDEF12-2222-3333-4444-555555555555-0",
		f"feature/ai-fix/{INCIDENT}-0",
	],
)
def test_anything_else_is_not_linked(name: str) -> None:
	"""Not linked beats linked to the wrong incident."""
	assert parse_fix_branch(name) is None
	assert not is_fix_branch(name)


def test_a_negative_attempt_is_refused() -> None:
	with pytest.raises(ValueError):
		format_fix_branch(INCIDENT, -1)

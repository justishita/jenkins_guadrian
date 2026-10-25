"""Draft-PR branch names: ``ai-fix/<incident_id>-<attempt>``.

**Owner:** P3. The branch name is how a Jenkins re-validation run is linked back to the
incident and remediation attempt that produced it, without any lookup table: the
pipeline knows its branch, and ``parse_fix_branch`` turns it back into
``(incident_id, attempt)``.

Lives in ``common/`` so the producer (the remediation engine) and any consumer - the
backend's validation webhook included, whose image ships only ``backend/`` and
``common/`` - share one definition. Format and parser are tested as a round trip.
"""

from __future__ import annotations

import re
from uuid import UUID


PREFIX = "ai-fix/"

_PATTERN = re.compile(
    r"^ai-fix/(?P<incident>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})-(?P<attempt>0|[1-9][0-9]*)$"
)


def format_fix_branch(incident_id: UUID | str, attempt: int) -> str:
    """Return ``ai-fix/<incident_id>-<attempt>`` with the full, lower-case incident id."""
    if attempt < 0:
        raise ValueError("attempt must be non-negative")
    return f"{PREFIX}{UUID(str(incident_id))}-{attempt}"


def parse_fix_branch(name: str) -> tuple[UUID, int] | None:
    """Return ``(incident_id, attempt)`` for an AI fix branch, or ``None`` for any other.

    Accepts an optional ``refs/heads/`` or ``origin/`` prefix, since that is how CI
    often reports a branch. Strict otherwise: a name it does not recognise is not
    linked, rather than linked to the wrong incident.
    """
    for prefix in ("refs/heads/", "origin/"):
        if name.startswith(prefix):
            name = name[len(prefix):]
    match = _PATTERN.match(name)
    if match is None:
        return None
    return UUID(match.group("incident")), int(match.group("attempt"))


def is_fix_branch(name: str) -> bool:
    return parse_fix_branch(name) is not None


__all__ = ["PREFIX", "format_fix_branch", "is_fix_branch", "parse_fix_branch"]

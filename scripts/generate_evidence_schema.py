"""Regenerate the canonical shared evidence schema from ``common.models.Evidence``.

**Owner:** P3. The checked-in ``common/evidence_schema.json`` is the contract every
agent writes against, so it must never drift from the Pydantic model that validates
those documents at runtime. This script is the single way to update it; running it
with ``--check`` is what ``tests/common/test_evidence_schema.py`` does in CI.

Usage::

    python scripts/generate_evidence_schema.py            # rewrite the schema file
    python scripts/generate_evidence_schema.py --check    # fail if it is stale
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
	sys.path.insert(0, str(REPOSITORY_ROOT))

from common.models import Evidence  # noqa: E402  (import needs the path above)


SCHEMA_PATH = REPOSITORY_ROOT / "common" / "evidence_schema.json"
SCHEMA_ID = "https://jenkinsguardians.local/schemas/evidence.json"
SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_DESCRIPTION = (
	"Canonical contract for one agent's findings about one incident. Each of the "
	"jenkins_agent, metrics_agent and code_agent writes exactly one document per "
	"incident; agents never read each other's documents directly. Generated from "
	"common.models.Evidence by scripts/generate_evidence_schema.py - edit the model, "
	"not this file. The evidence store wraps a stored document with an integer "
	"'version' field that is not part of this contract."
)


def build_schema() -> dict[str, Any]:
	"""Return the canonical JSON Schema document for the evidence contract."""
	schema: dict[str, Any] = Evidence.model_json_schema(mode="serialization")
	ordered: dict[str, Any] = {
		"$schema": SCHEMA_DIALECT,
		"$id": SCHEMA_ID,
		"title": "Evidence",
		"description": SCHEMA_DESCRIPTION,
	}
	for key, value in schema.items():
		if key in ordered:
			continue
		ordered[key] = value
	return ordered


def serialize(schema: dict[str, Any]) -> str:
	"""Render the schema the way it is stored on disk, with a trailing newline."""
	return json.dumps(schema, indent=2, ensure_ascii=True, sort_keys=False) + "\n"


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument(
		"--check",
		action="store_true",
		help="exit non-zero if the checked-in schema differs from the model",
	)
	arguments = parser.parse_args(argv)

	expected = serialize(build_schema())
	if arguments.check:
		current = SCHEMA_PATH.read_text(encoding="utf-8") if SCHEMA_PATH.exists() else ""
		if current != expected:
			print(
				f"{SCHEMA_PATH.relative_to(REPOSITORY_ROOT)} is stale; "
				"run python scripts/generate_evidence_schema.py",
				file=sys.stderr,
			)
			return 1
		print("evidence schema is up to date")
		return 0

	SCHEMA_PATH.write_text(expected, encoding="utf-8")
	print(f"wrote {SCHEMA_PATH.relative_to(REPOSITORY_ROOT)}")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

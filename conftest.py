"""Repository-wide pytest setup.

Runs before any test module is imported, which matters for the two things here: both
are read once at import time by code under test, so setting them later is too late.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil


REPO_ROOT = Path(__file__).resolve().parent

# backend.config builds `settings` at import, and the webhook route rejects every
# request when no secret is configured. tests/test_backend_webhooks.py sends this
# exact value as its fallback token, so a fresh clone with no .env would otherwise
# fail six tests with 401. setdefault: a real WEBHOOK_SHARED_SECRET still wins.
os.environ.setdefault("WEBHOOK_SHARED_SECRET", "a8f9c2d1e3f4b5a6c7d8e9f0a1b2c3d4")


def _find_opa() -> str | None:
	"""Locate an opa binary so real-policy tests run instead of silently skipping.

	Order: an explicit OPA_BIN, then opa on PATH, then the repo-local copy that
	``python scripts/get_opa.py`` downloads into ``.bin/``.
	"""
	explicit = os.environ.get("OPA_BIN")
	if explicit and Path(explicit).is_file():
		return explicit
	on_path = shutil.which("opa")
	if on_path:
		return on_path
	for name in ("opa.exe", "opa"):
		local = REPO_ROOT / ".bin" / name
		if local.is_file():
			return str(local)
	return None


_opa = _find_opa()
if _opa:
	os.environ["OPA_BIN"] = _opa

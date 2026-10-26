"""Run the ``opa`` CLI through Docker, so tests need no local OPA binary.

**Owner:** P2. The policy tests call ``$OPA_BIN eval ... -d <repo>/policies``. Point ``OPA_BIN`` at
``scripts/opa.cmd`` (Windows) and this wrapper runs the same command in the OPA image the compose file
uses, mounting ``policies/`` at ``/policies`` and rewriting that path in the arguments. Standard input
is passed through (``--stdin-input``), so the calls behave exactly like a local binary.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
POLICIES = REPO / "policies"
IMAGE = os.environ.get("OPA_IMAGE", "openpolicyagent/opa:1.21.1-static")


def translate(arguments: list[str]) -> list[str]:
    """Replace the host policies path with the in-container mount point."""
    host = {str(POLICIES), str(POLICIES).replace("\\", "/"), POLICIES.as_posix()}
    return ["/policies" if argument in host else argument for argument in arguments]


def command(arguments: list[str]) -> list[str]:
    return ["docker", "run", "--rm", "-i", "-v", f"{POLICIES}:/policies:ro", IMAGE, *translate(arguments)]


def main() -> int:
    return subprocess.run(command(sys.argv[1:]), check=False).returncode


if __name__ == "__main__":
    sys.exit(main())

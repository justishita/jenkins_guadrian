"""Run every policy test, with or without a local OPA binary.

**Owner:** P2. Usage::

    python scripts/run_policy_tests.py

1. The Rego unit tests (``policies/*_test.rego``) via ``opa test``.
2. The Python tests that call the real policies through the OPA CLI (``tests/coordinator/test_policy.py``,
   ``test_remediation_gate.py``, ``tests/code_agent/test_remediation.py``). These skip when no ``opa`` binary
   is found; here ``OPA_BIN`` is pointed at the Docker wrapper so they run instead.

Needs Docker with the OPA image (the same one ``docker-compose.yml`` uses). Exit status 1 if anything fails
or if the OPA-backed Python tests were skipped (a skipped policy test proves nothing).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WRAPPER = REPO / "scripts" / ("opa.cmd" if os.name == "nt" else "opa_docker.py")
TESTS = [
    "tests/coordinator/test_policy.py",
    "tests/coordinator/test_remediation_gate.py",
    "tests/code_agent/test_remediation.py",
    "tests/coordinator/test_authority.py",
    "tests/coordinator/test_policy_floor.py",
]


def rego_unit_tests() -> int:
    print("== Rego unit tests (opa test) ==")
    sys.path.insert(0, str(REPO / "scripts"))
    from opa_docker import command

    return subprocess.run(command(["test", "/policies", "-v"]), check=False).returncode


def python_tests() -> int:
    print("\n== Python tests against the real policies ==")
    env = {**os.environ, "OPA_BIN": str(WRAPPER), "PYTHONUTF8": "1"}
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", *TESTS, "-q", "-rs"], cwd=REPO, env=env, capture_output=True, text=True, check=False
    )
    print(completed.stdout[-3000:])
    skipped_opa = len(re.findall(r"SKIPPED.*OPA", completed.stdout))
    if skipped_opa:
        print(f"FAIL: {skipped_opa} OPA test(s) were skipped; the wrapper did not provide a working opa")
        return 1
    return completed.returncode


def main() -> int:
    return 1 if (rego_unit_tests() | python_tests()) else 0


if __name__ == "__main__":
    sys.exit(main())

"""Create and push controlled Jenkins fault-injection branches.

Usage:
    python scripts/inject_fault.py tc01_unit_test
    python scripts/inject_fault.py tc02_compile_syntax
    python scripts/inject_fault.py tc02_compile_import
    python scripts/inject_fault.py revert fault/tc01_unit_test-<timestamp>
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import NamedTuple
from urllib.parse import quote


LOGGER = logging.getLogger("inject_fault")
REPO_ROOT = Path(__file__).resolve().parents[1]
TARGET_APP = Path("target_app")
SCENARIOS_DIR = Path("scenarios")
SUPPORTED_SCENARIOS = (
    "tc01_unit_test",
    "tc02_compile_syntax",
    "tc02_compile_import",
)
MISSING_MODULE_IMPORT = "import _jenkins_fault_injection_missing_module_"
COAUTHOR_TRAILER = "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"


class FaultInfo(NamedTuple):
    file: str
    line: int
    expected_failure_type: str


class GitCommandError(RuntimeError):
    """A Git command failed without exposing its captured output."""


def _git(repo: Path, *args: str, check: bool = True) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GitCommandError(f"could not run git {args[0]}") from error
    if check and result.returncode != 0:
        raise GitCommandError(
            f"git {args[0]} failed with exit code {result.returncode}"
        )
    return result.stdout.strip()


def _validate_repository(repo: Path, *, allow_main: bool = False) -> str:
    root = Path(_git(repo, "rev-parse", "--show-toplevel")).resolve()
    if root != repo.resolve():
        raise RuntimeError("script must run from the root of its Git repository")
    branch = _git(repo, "branch", "--show-current")
    if not branch:
        raise RuntimeError("refusing to run from detached HEAD; check out a branch first")
    if branch == "main" and not allow_main:
        raise RuntimeError("refusing to run on main")
    return branch


def _require_clean_tree(repo: Path) -> None:
    if _git(repo, "status", "--porcelain", "--untracked-files=all"):
        raise RuntimeError("working tree must be clean before fault injection")


def _read_lines(path: Path) -> list[str]:
    return path.read_bytes().decode("utf-8").splitlines(keepends=True)


def _write_lines(path: Path, lines: list[str]) -> None:
    path.write_bytes("".join(lines).encode("utf-8"))


def _line_number(lines: list[str], expression: str) -> int:
    matches = [
        index + 1 for index, line in enumerate(lines)
        if re.search(expression, line)
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"expected exactly one changed line matching {expression!r}; found {len(matches)}"
        )
    return matches[0]


def _replace_idempotently(path: Path, original: str, faulted: str) -> int:
    lines = _read_lines(path)
    original_indexes = [i for i, line in enumerate(lines) if line.strip() == original]
    faulted_indexes = [i for i, line in enumerate(lines) if line.strip() == faulted]
    if len(faulted_indexes) == 1 and not original_indexes:
        return faulted_indexes[0] + 1
    if len(original_indexes) != 1 or faulted_indexes:
        raise RuntimeError(f"expected one clean, known source line in {path}")
    index = original_indexes[0]
    lines[index] = lines[index].replace(original, faulted, 1)
    _write_lines(path, lines)
    return index + 1


def apply_fault(repo: Path, scenario: str) -> FaultInfo:
    """Apply one whitelisted fault, returning its ground-truth location."""
    if scenario not in SUPPORTED_SCENARIOS:
        raise ValueError(f"unsupported scenario: {scenario}")

    if scenario == "tc01_unit_test":
        relative_file = TARGET_APP / "tests" / "test_orders.py"
        path = repo / relative_file
        line = _replace_idempotently(
            path,
            'assert order["quantity"] == 3',
            'assert order["quantity"] == 4',
        )
        failure_type = "code_test_failure"
    elif scenario == "tc02_compile_syntax":
        relative_file = TARGET_APP / "app" / "main.py"
        path = repo / relative_file
        line = _replace_idempotently(
            path,
            "app = create_app()",
            "app = create_app(",
        )
        failure_type = "build_compilation_failure"
    else:
        relative_file = TARGET_APP / "app" / "main.py"
        path = repo / relative_file
        lines = _read_lines(path)
        imports = [
            index for index, source_line in enumerate(lines)
            if source_line.strip() == MISSING_MODULE_IMPORT
        ]
        if len(imports) == 1:
            line = imports[0] + 1
        elif imports:
            raise RuntimeError(f"found duplicate injected imports in {path}")
        else:
            insertion = next(
                (
                    index
                    for index, source_line in enumerate(lines)
                    if source_line.startswith(("import ", "from "))
                ),
                None,
            )
            if insertion is None:
                raise RuntimeError(f"no import section found in {path}")
            newline = "\r\n" if lines[insertion].endswith("\r\n") else "\n"
            lines.insert(insertion, MISSING_MODULE_IMPORT + newline)
            _write_lines(path, lines)
            line = insertion + 1
        failure_type = "dependency_regression"

    return FaultInfo(relative_file.as_posix(), line, failure_type)


def _write_ground_truth(repo: Path, scenario: str, info: FaultInfo) -> Path:
    path = repo / SCENARIOS_DIR / f"{scenario}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "scenario": scenario,
        "file": info.file,
        "line": info.line,
        "expected_failure_type": info.expected_failure_type,
    }
    temporary = path.with_suffix(".json.tmp")
    try:
        temporary.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return path


def _fault_branch_exists(repo: Path, branch: str) -> bool:
    local = _git(repo, "show-ref", "--verify", f"refs/heads/{branch}", check=False)
    if local:
        return True
    remote = _git(
        repo,
        "ls-remote",
        "--exit-code",
        "--heads",
        "origin",
        f"refs/heads/{branch}",
        check=False,
    )
    return bool(remote)


def create_fault_branch(repo: Path, scenario: str) -> str:
    if scenario not in SUPPORTED_SCENARIOS:
        raise ValueError(f"unsupported scenario: {scenario}")
    _validate_repository(repo)
    _require_clean_tree(repo)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    branch = f"fault/{scenario}-{timestamp}"
    if _fault_branch_exists(repo, branch):
        raise RuntimeError(f"branch already exists: {branch}")

    _git(repo, "switch", "-c", branch)
    info = apply_fault(repo, scenario)
    ground_truth = _write_ground_truth(repo, scenario, info)
    target_file = repo / info.file
    relative_ground_truth = ground_truth.relative_to(repo).as_posix()

    messages = {
        "tc01_unit_test": "test: introduce assertion fault in sample orders",
        "tc02_compile_syntax": "build: introduce syntax error in sample app",
        "tc02_compile_import": "build: introduce missing dependency in sample app",
    }
    _git(repo, "add", "--", info.file, relative_ground_truth)
    _git(
        repo,
        "commit",
        "-m",
        messages[scenario],
        "-m",
        COAUTHOR_TRAILER,
    )
    _git(repo, "push", "--set-upstream", "origin", branch)

    jenkins_url = os.getenv("JENKINS_URL", "http://localhost:8080").rstrip("/")
    build_url = (
        f"{jenkins_url}/job/target-app/job/{quote(branch, safe='')}/"
    )
    LOGGER.info("Injected %s into %s:%d", scenario, info.file, info.line)
    LOGGER.info("Ground truth: %s", relative_ground_truth)
    LOGGER.info("Jenkins build URL: %s", build_url)
    LOGGER.info("Pushed branch: %s", branch)
    return branch


def revert_fault_branch(repo: Path, branch: str) -> None:
    _validate_repository(repo, allow_main=True)
    if not branch.startswith("fault/") or branch.count("/") != 1:
        raise ValueError("revert requires an exact fault/<scenario>-<timestamp> branch name")
    if _git(repo, "branch", "--show-current") == branch:
        raise RuntimeError("check out another branch before deleting the fault branch")

    remote = _git(
        repo,
        "ls-remote",
        "--exit-code",
        "--heads",
        "origin",
        f"refs/heads/{branch}",
        check=False,
    )
    if remote:
        _git(repo, "push", "origin", "--delete", branch)
    else:
        LOGGER.info("Remote branch does not exist: %s", branch)

    local = _git(repo, "show-ref", "--verify", f"refs/heads/{branch}", check=False)
    if local:
        _git(repo, "branch", "-D", branch)
    else:
        LOGGER.info("Local branch does not exist: %s", branch)
    LOGGER.info("Reverted fault branch: %s", branch)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=(*SUPPORTED_SCENARIOS, "revert"))
    parser.add_argument("branch", nargs="?", help="Exact fault branch name for revert")
    args = parser.parse_args(argv)
    try:
        if args.command == "revert":
            if not args.branch:
                parser.error("revert requires an exact branch name, e.g. fault/tc01_unit_test-...")
            revert_fault_branch(REPO_ROOT, args.branch)
        else:
            if args.branch:
                parser.error("scenario commands do not take a branch argument")
            create_fault_branch(REPO_ROOT, args.command)
    except (GitCommandError, OSError, RuntimeError, ValueError) as error:
        LOGGER.error("Fault injection failed: %s", error)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

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
    "tc10_flaky",
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
    elif scenario == "tc10_flaky":
        relative_file = TARGET_APP / "tests" / "test_orders.py"
        path = repo / relative_file
        lines = _read_lines(path)
        flaky_marker = "def test_flaky_random_order_check"
        existing_idx = next(
            (i for i, l in enumerate(lines) if flaky_marker in l),
            None,
        )
        if existing_idx is not None:
            assert_idx = next(
                (
                    i
                    for i in range(existing_idx, min(len(lines), existing_idx + 10))
                    if "random.random() < 0.5" in lines[i]
                ),
                existing_idx + 2,
            )
            line = assert_idx + 1
        else:
            newline = "\r\n" if (lines and lines[-1].endswith("\r\n")) else "\n"
            if lines and not lines[-1].endswith(("\n", "\r")):
                lines[-1] = lines[-1] + newline
            flaky_snippet = [
                newline,
                f"def test_flaky_random_order_check() -> None:{newline}",
                f"    import random{newline}",
                f"    assert random.random() < 0.5{newline}",
            ]
            lines.extend(flaky_snippet)
            _write_lines(path, lines)
            line = len(lines)
        failure_type = "flaky_test"
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
        "tc10_flaky": "test: introduce non-deterministic assertion in sample orders",
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


def _multibranch_build_url(jenkins_url: str, branch: str, job_token: str | None = None) -> str:
    """Jenkins multibranch jobs are triggered at /job/target-app/job/<branch>/build."""
    encoded_branch = quote(branch, safe="").replace("%2F", "%252F")
    trigger_url = f"{jenkins_url.rstrip('/')}/job/target-app/job/{encoded_branch}/build"
    if job_token:
        trigger_url = f"{trigger_url}?token={quote(job_token, safe='')}"
    return trigger_url


def trigger_multibranch_build(
    branch: str,
    *,
    runs: int = 8,
    jenkins_url: str | None = None,
    username: str | None = None,
    api_token: str | None = None,
    job_token: str | None = None,
) -> list[str]:
    """Trigger ~8 builds of the multibranch job via the Jenkins HTTP POST API.

    Authentication uses ``JENKINS_USER`` / ``JENKINS_API_TOKEN`` (Basic auth).
    An optional job trigger token is sent as ``?token=`` (``JENKINS_JOB_TOKEN``).
    CSRF crumbs are fetched from ``/crumbIssuer/api/json`` when available.
    """
    import base64
    import urllib.error
    import urllib.request

    url_base = (jenkins_url or os.getenv("JENKINS_URL", "http://localhost:8080")).rstrip("/")
    user = username or os.getenv("JENKINS_USER", "")
    token = api_token or os.getenv("JENKINS_API_TOKEN", "")
    remote_token = job_token if job_token is not None else os.getenv("JENKINS_JOB_TOKEN", "")

    trigger_url = _multibranch_build_url(url_base, branch, remote_token or None)
    LOGGER.info("Triggering %d builds on %s", runs, trigger_url)
    triggered: list[str] = []

    headers: dict[str, str] = {}
    if user and token:
        headers["Authorization"] = (
            "Basic " + base64.b64encode(f"{user}:{token}".encode("utf-8")).decode("ascii")
        )
    elif not remote_token:
        LOGGER.warning("No Jenkins credentials or job token set; POST /build may be rejected")

    try:
        crumb_req = urllib.request.Request(f"{url_base}/crumbIssuer/api/json")
        for header_name, header_value in headers.items():
            crumb_req.add_header(header_name, header_value)
        with urllib.request.urlopen(crumb_req, timeout=10) as crumb_resp:
            crumb_payload = json.loads(crumb_resp.read().decode("utf-8"))
        crumb_field = crumb_payload.get("crumbRequestField")
        crumb_value = crumb_payload.get("crumb")
        if crumb_field and crumb_value:
            headers[str(crumb_field)] = str(crumb_value)
    except Exception as exc:
        LOGGER.debug("Jenkins crumb not available (%s); continuing without it", exc)

    for run_i in range(runs):
        req = urllib.request.Request(trigger_url, data=b"", method="POST")
        for header_name, header_value in headers.items():
            req.add_header(header_name, header_value)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                loc = resp.headers.get("Location", trigger_url)
                triggered.append(loc)
                LOGGER.info("Triggered run %d/%d (status %d)", run_i + 1, runs, resp.status)
        except urllib.error.HTTPError as http_err:
            LOGGER.warning("Trigger run %d returned HTTP %d: %s", run_i + 1, http_err.code, http_err.reason)
        except Exception as exc:
            LOGGER.warning("Trigger run %d failed: %s", run_i + 1, exc)

    return triggered


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
    parser.add_argument("command", choices=(*SUPPORTED_SCENARIOS, "revert", "trigger"))
    parser.add_argument("branch", nargs="?", help="Exact fault branch name for revert or trigger")
    parser.add_argument(
        "--runs",
        type=int,
        default=8,
        help="Number of builds to trigger (default: 8)",
    )
    parser.add_argument(
        "--trigger",
        action="store_true",
        help="Trigger builds via Jenkins API after pushing",
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "revert":
            if not args.branch:
                parser.error("revert requires an exact branch name, e.g. fault/tc01_unit_test-...")
            revert_fault_branch(REPO_ROOT, args.branch)
        elif args.command == "trigger":
            if not args.branch:
                parser.error("trigger requires a branch name, e.g. fault/tc10_flaky-...")
            trigger_multibranch_build(args.branch, runs=args.runs)
        else:
            if args.branch:
                parser.error("scenario commands do not take a branch argument")
            created_branch = create_fault_branch(REPO_ROOT, args.command)
            if args.trigger or args.command == "tc10_flaky":
                trigger_multibranch_build(created_branch, runs=args.runs)
    except (GitCommandError, OSError, RuntimeError, ValueError) as error:
        LOGGER.error("Fault injection failed: %s", error)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

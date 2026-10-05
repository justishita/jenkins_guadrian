"""Inject and revert the code-side fault scenarios owned by P3.

**Owner:** P3. Companion to ``scripts/inject_faults.py``, which drives the runtime
faults (latency, CPU, memory) that the metrics agent investigates. These faults are
*changes to the repository* instead: they break the target application the way a real
commit would, so the pipeline fails and the Code agent has something true to find.

Every edit is backed up before it is made, and ``revert`` restores from those backups.
Nothing here touches git, so an unrelated work-in-progress is never discarded.

    python scripts/inject_code_faults.py tc03     # dependency regression
    python scripts/inject_code_faults.py tc08     # configuration error
    python scripts/inject_code_faults.py status   # what is currently injected
    python scripts/inject_code_faults.py revert   # restore everything

The usual loop is: inject, commit and push so Jenkins builds it, let the agents
investigate, then revert.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import shutil
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BACKUP_DIR = REPOSITORY_ROOT / "data" / "fault_backups"
STATE_PATH = BACKUP_DIR / "state.json"

REQUIREMENTS = Path("target_app/requirements.txt")
SETTINGS = Path("target_app/config/settings.yaml")


@dataclass(frozen=True)
class Edit:
    """One substitution in one file, with the expectation that it actually matched."""

    path: Path
    pattern: str
    replacement: str
    description: str

    def apply(self, text: str) -> str:
        updated, count = re.subn(self.pattern, self.replacement, text, count=1, flags=re.MULTILINE)
        if count != 1:
            raise SystemExit(
                f"could not apply the fault to {self.path}: "
                f"pattern {self.pattern!r} matched {count} times, expected 1. "
                "The file may already be modified - run `revert` first."
            )
        return updated


@dataclass(frozen=True)
class Scenario:
    identifier: str
    title: str
    expectation: str
    edits: tuple[Edit, ...]


SCENARIOS: dict[str, Scenario] = {
    "tc03": Scenario(
        identifier="TC-03",
        title="Dependency Regression",
        expectation=(
            "The Code agent should classify this as dependency_regression and recommend "
            "pinning httpx back to 0.28.1."
        ),
        edits=(
            Edit(
                path=REQUIREMENTS,
                pattern=r"^httpx==0\.28\.1$",
                # A version that does not exist: pip install fails outright, which is a
                # cleaner, faster signal than an incompatible-but-installable release.
                replacement="httpx==0.99.0",
                description="upgrade httpx to an incompatible version",
            ),
        ),
    ),
    "tc08": Scenario(
        identifier="TC-08",
        title="Configuration Error",
        expectation=(
            "The Code agent should classify this as config_error and recommend restoring "
            "database_timeout_seconds to 3."
        ),
        edits=(
            Edit(
                path=SETTINGS,
                pattern=r"^database_timeout_seconds: 3$",
                # Settings declares this gt=0, so start-up fails its own validation
                # rather than failing somewhere unrelated later.
                replacement="database_timeout_seconds: -5",
                description="set database_timeout_seconds to a non-positive value",
            ),
        ),
    ),
}


def _read_state() -> dict[str, list[str]]:
    if not STATE_PATH.is_file():
        return {}
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return state if isinstance(state, dict) else {}


def _write_state(state: dict[str, list[str]]) -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _backup_path(path: Path) -> Path:
    return BACKUP_DIR / path.as_posix().replace("/", "__")


def inject(scenario: Scenario) -> int:
    state = _read_state()
    if scenario.identifier in state:
        print(f"{scenario.identifier} is already injected; run `revert` first.", file=sys.stderr)
        return 1

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    touched: list[str] = []
    for edit in scenario.edits:
        absolute = REPOSITORY_ROOT / edit.path
        if not absolute.is_file():
            print(f"{edit.path} does not exist", file=sys.stderr)
            return 1

        backup = _backup_path(edit.path)
        if not backup.exists():
            shutil.copy2(absolute, backup)

        original = absolute.read_text(encoding="utf-8")
        absolute.write_text(edit.apply(original), encoding="utf-8")
        touched.append(edit.path.as_posix())
        print(f"  {edit.path}: {edit.description}")

    state[scenario.identifier] = touched
    _write_state(state)

    print(f"\nInjected {scenario.identifier} ({scenario.title}).")
    print(scenario.expectation)
    print("\nCommit and push to trigger the pipeline, then `revert` when you are done.")
    return 0


def revert() -> int:
    state = _read_state()
    if not state:
        print("Nothing is injected.")
        return 0

    for identifier, paths in sorted(state.items()):
        for relative in paths:
            backup = _backup_path(Path(relative))
            if not backup.is_file():
                print(f"  no backup for {relative}; leaving it alone", file=sys.stderr)
                continue
            shutil.copy2(backup, REPOSITORY_ROOT / relative)
            backup.unlink()
            print(f"  restored {relative}")
        print(f"Reverted {identifier}.")

    STATE_PATH.unlink(missing_ok=True)
    return 0


def status() -> int:
    state = _read_state()
    if not state:
        print("Nothing is injected.")
        return 0
    for identifier, paths in sorted(state.items()):
        print(f"{identifier}: {', '.join(paths)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=[*sorted(SCENARIOS), "revert", "status"],
        help="a scenario to inject, or revert/status",
    )
    arguments = parser.parse_args(argv)

    if arguments.action == "revert":
        return revert()
    if arguments.action == "status":
        return status()
    return inject(SCENARIOS[arguments.action])


if __name__ == "__main__":
    raise SystemExit(main())

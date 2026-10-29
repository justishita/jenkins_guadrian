"""Delete old per-incident evidence and blackboard files.

**Owner:** P2 (Week 7). Evidence and blackboard documents only matter while an incident is being
investigated and reviewed; the audit trail (``data/audit``, the ``audit_events`` table) is the long-term
record and is never touched here. Usage::

    python scripts/prune_evidence.py                          # dry run: list what would go (older than 30 days)
    python scripts/prune_evidence.py --older-than-days 7
    python scripts/prune_evidence.py --apply                  # actually delete
    python scripts/prune_evidence.py --root data/evidence --apply

A directory is pruned only if it is named like an incident id (a UUID) and *nothing* inside it was changed
within the window, so an incident that is still being worked on is left alone.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

REPO = Path(__file__).resolve().parents[1]
DEFAULT_ROOTS = (REPO / "data" / "evidence", REPO / "data" / "blackboard")


@dataclass(frozen=True)
class Candidate:
    path: Path
    age_days: float
    files: int


def is_incident_directory(path: Path) -> bool:
    try:
        UUID(path.name)
    except ValueError:
        return False
    return path.is_dir() and not path.is_symlink()


def newest_change(directory: Path) -> float | None:
    times = [item.stat().st_mtime for item in directory.rglob("*") if item.is_file() and not item.is_symlink()]
    return max(times) if times else None


def find_old(roots: list[Path], older_than_days: float, now: float | None = None) -> list[Candidate]:
    now = time.time() if now is None else now
    cutoff = now - older_than_days * 86400
    found: list[Candidate] = []
    for root in roots:
        if not root.is_dir():
            continue
        for directory in sorted(root.iterdir()):
            if not is_incident_directory(directory):
                continue
            newest = newest_change(directory)
            if newest is None or newest < cutoff:
                age = (now - (newest if newest is not None else directory.stat().st_mtime)) / 86400
                found.append(Candidate(directory, age, sum(1 for item in directory.rglob("*") if item.is_file())))
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--older-than-days", type=float, default=30.0)
    parser.add_argument("--root", action="append", type=Path, help="evidence directory to prune (repeatable); default: data/evidence and data/blackboard")
    parser.add_argument("--apply", action="store_true", help="delete; without this the run only lists")
    args = parser.parse_args(argv)
    if args.older_than_days < 0:
        parser.error("--older-than-days must not be negative")

    candidates = find_old(args.root or list(DEFAULT_ROOTS), args.older_than_days)
    for item in candidates:
        print(f"{'delete' if args.apply else 'would delete'}  {item.path}  ({item.files} file(s), {item.age_days:.0f} days old)")
        if args.apply:
            shutil.rmtree(item.path)
    verb = "deleted" if args.apply else "would delete"
    print(f"{verb} {len(candidates)} incident folder(s)" + ("" if args.apply or not candidates else "; run again with --apply"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

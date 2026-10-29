"""Check that incident audit trails are complete and consistent.

**Owner:** P2. Usage::

    python scripts/check_audit.py --latest                 # newest incident in data/audit
    python scripts/check_audit.py <incident_id>
    python scripts/check_audit.py --all                    # every incident
    python scripts/check_audit.py --all --unaudited-ok jenkins_agent

Reads the PostgreSQL trail when ``DATABASE_URL`` is set, otherwise the JSON-lines files under
``data/audit``. Exit status 1 if any *error* is found; warnings (a trail that lacks the remediation gate's events) are printed but do not fail the run. The rules are in ``common/audit_check.py``;
``--unaudited-ok`` names agents that are known not to write audit records yet, so a known gap is
reported once instead of failing every run.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from uuid import UUID

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from common.audit import AuditLog, build_audit_log
from common.audit_check import check_incident, errors

AUDIT_DIR = REPO / "data" / "audit"


async def incident_ids(database_url: str | None) -> list[UUID]:
    if database_url:
        import asyncpg

        connection = await asyncpg.connect(database_url.replace("postgresql+asyncpg://", "postgresql://", 1))
        try:
            rows = await connection.fetch("SELECT DISTINCT incident_id FROM audit_events")
        finally:
            await connection.close()
        return [UUID(str(row["incident_id"])) for row in rows]
    files = sorted(AUDIT_DIR.glob("*.jsonl"), key=lambda path: path.stat().st_mtime)
    return [UUID(path.stem) for path in files]


async def run(args: argparse.Namespace) -> int:
    database_url = os.getenv("DATABASE_URL") or None
    audit: AuditLog = build_audit_log(database_url=database_url, root=AUDIT_DIR, best_effort=False)
    if args.incident:
        ids = [UUID(args.incident)]
    else:
        ids = await incident_ids(database_url)
        if not ids:
            print("no audited incidents found")
            return 1
        if args.latest:
            ids = ids[-1:]
    failed = 0
    for incident in ids:
        problems = check_incident(await audit.read(incident), unaudited_ok=args.unaudited_ok)
        hard = errors(problems)
        status = "ok" if not problems else f"{len(hard)} error(s), {len(problems) - len(hard)} warning(s)"
        print(f"{incident}: {status}")
        for problem in problems:
            if problem.severity == "error" or not args.errors_only:
                print(f"  {problem}")
        failed += bool(hard)
    return 1 if failed else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("incident", nargs="?")
    parser.add_argument("--latest", action="store_true")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--errors-only", action="store_true", help="do not print warnings")
    parser.add_argument("--unaudited-ok", nargs="*", default=[], metavar="AGENT")
    args = parser.parse_args()
    if not (args.incident or args.latest or args.all):
        parser.error("give an incident id, --latest or --all")
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()

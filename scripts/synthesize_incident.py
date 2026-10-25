"""Build or show the unified incident report for one incident.

**Owner:** P2. Two modes::

    python scripts/synthesize_incident.py <incident_id>          # fuse the evidence on disk now
    python scripts/synthesize_incident.py --latest               # newest incident under data/evidence
    python scripts/synthesize_incident.py <incident_id> --wait 90   # wait for the Coordinator
                                                                    # service to write it, then show it

Without ``--wait`` this runs the same ``CoordinatorAgent.synthesize`` the service runs, locally,
against ``data/evidence`` - handy for replaying a stored incident without the stack. It writes
``data/reports/<id>.{json,md}`` and prints the Markdown report.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path
from uuid import UUID

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from common.audit import build_audit_log
from common.evidence_store import FileEvidenceStore
from coordinator.agent import CoordinatorAgent
from coordinator.report import render_markdown
from coordinator.report_store import ReportStore

DATA = REPO / "data"


def latest_incident(evidence_root: Path) -> UUID:
    folders = [p for p in evidence_root.iterdir() if p.is_dir()]
    if not folders:
        raise SystemExit(f"no incidents under {evidence_root}")
    newest = max(folders, key=lambda p: max((f.stat().st_mtime for f in p.glob("*.json")), default=0))
    return UUID(newest.name)


async def synthesize(incident: UUID) -> str:
    coordinator = CoordinatorAgent(
        FileEvidenceStore(DATA / "evidence"),
        report_store=ReportStore(DATA / "reports"),
        audit=build_audit_log(root=DATA / "audit"),
    )
    return render_markdown(await coordinator.synthesize(incident))


def wait_for_report(incident: UUID, seconds: float) -> str:
    path = ReportStore(DATA / "reports").path(incident, "md")
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.is_file():
            return path.read_text(encoding="utf-8")
        time.sleep(1)
    raise SystemExit(f"no report at {path} after {seconds:.0f}s - is the coordinator service running?")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("incident_id", nargs="?")
    parser.add_argument("--latest", action="store_true")
    parser.add_argument("--wait", type=float, metavar="SECONDS")
    args = parser.parse_args()
    if bool(args.incident_id) == args.latest:
        parser.error("give an incident id or --latest")
    incident = latest_incident(DATA / "evidence") if args.latest else UUID(args.incident_id)
    print(wait_for_report(incident, args.wait) if args.wait else asyncio.run(synthesize(incident)))


if __name__ == "__main__":
    main()

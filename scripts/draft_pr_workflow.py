#!/usr/bin/env python3
"""Substitute Draft-PR Workflow for Remediation Validation.

Creates and manages draft pull requests on branches named
``ai-fix/<incident_id>-<attempt>`` so that Jenkins validation can automatically link
back to the corresponding incident.

Usage:
    python scripts/draft_pr_workflow.py create --incident-id <UUID> --attempt <N> --base-ref <SHA>
    python scripts/draft_pr_workflow.py parse --branch <BRANCH_NAME>
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from uuid import UUID

from common.fix_branch import format_fix_branch, is_fix_branch, parse_fix_branch

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def cmd_create(args: argparse.Namespace) -> int:
    incident_id = UUID(str(args.incident_id))
    attempt = int(args.attempt)
    branch = format_fix_branch(incident_id, attempt)

    logger.info("Draft PR workflow: creating branch '%s' from base ref '%s'", branch, args.base_ref)
    
    output = {
        "incident_id": str(incident_id),
        "attempt": attempt,
        "branch_name": branch,
        "base_ref": args.base_ref,
        "is_draft": True,
        "auto_merge": False,
        "pr_title": f"fix(remediation): incident {str(incident_id)[:8]} (attempt {attempt})",
        "validation_url_pattern": f"/job/target-app/job/{branch.replace('/', '%252F')}/build",
    }
    
    if args.json:
        print(json.dumps(output, indent=2))
    else:
        print(f"Branch: {branch}")
        print(f"Incident: {incident_id}")
        print(f"Attempt: {attempt}")
        print(f"Draft PR: Always opened as DRAFT")
        print(f"Validation link: {branch} -> incident {incident_id}")

    return 0


def cmd_parse(args: argparse.Namespace) -> int:
    parsed = parse_fix_branch(args.branch)
    if parsed is None:
        logger.error("Branch '%s' does not match pattern 'ai-fix/<incident_id>-<attempt>'", args.branch)
        return 1
    
    incident_id, attempt = parsed
    output = {
        "branch": args.branch,
        "incident_id": str(incident_id),
        "attempt": attempt,
        "valid": True,
    }
    if args.json:
        print(json.dumps(output, indent=2))
    else:
        print(f"Incident ID: {incident_id}")
        print(f"Attempt: {attempt}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Substitute draft-PR workflow")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser("create", help="Generate draft PR branch and metadata")
    create_parser.add_argument("--incident-id", required=True, help="Incident UUID")
    create_parser.add_argument("--attempt", type=int, default=0, help="Attempt number")
    create_parser.add_argument("--base-ref", default="main", help="Base git commit or branch")
    create_parser.add_argument("--json", action="store_true", help="Output JSON")

    parse_parser = subparsers.add_parser("parse", help="Parse incident ID and attempt from branch name")
    parse_parser.add_argument("--branch", required=True, help="Branch name to parse")
    parse_parser.add_argument("--json", action="store_true", help="Output JSON")

    args = parser.parse_args()
    if args.command == "create":
        return cmd_create(args)
    elif args.command == "parse":
        return cmd_parse(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())

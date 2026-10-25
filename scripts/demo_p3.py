"""End-to-end demo of the P3 pipeline: investigation -> fusion -> fix -> policy -> draft PR.

**Owner:** P3. One command, no Docker, no Jenkins, no network, no API key:

    python scripts/get_opa.py     # once: fetches the pinned, checksum-verified opa
    python scripts/demo_p3.py

What is real and what is simulated - say this when presenting:

* **Real:** the CrewAI code agent and its reasoning, the A2A notifications, the Google
  ADK Coordinator, synthesis, the remediation engine, the audit trail, and the OPA
  policy decisions, which run the actual Rego in ``policies/`` through the opa CLI.
* **Simulated:** GitHub is an in-memory fake, so no repository is touched. The Jenkins
  and metrics agents' evidence is recorded - their real versions need a live Jenkins,
  Prometheus and an LLM key - and is labelled as recorded wherever it appears.

Four scenarios, each with stated expectations. The script exits non-zero if any
expectation fails, so it doubles as a smoke test to run just before presenting.
"""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone
import hashlib
import logging
import os
from pathlib import Path
import shutil
import sys
import tempfile
import textwrap
from typing import Any, Callable
from uuid import UUID
import warnings


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Third-party import-time chatter (google-cloud deprecation notices) is not part of the
# demo. Agent errors still reach the console through logging at ERROR.
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)
logging.basicConfig(level=logging.ERROR, format="        ! %(name)s: %(message)s")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

import httpx  # noqa: E402

from agents.code_agent.agent import CodeInvestigationAgent  # noqa: E402
from agents.code_agent.remediation import FileEdit, FixPlan, NoFix, propose_fix  # noqa: E402
from agents.code_agent.tools.github_client import GitHubClient  # noqa: E402
from agents.code_agent.tools.github_writer import GitHubWriter  # noqa: E402
from common.a2a_contract import evidence_ready  # noqa: E402
from common.audit import FileAuditLog  # noqa: E402
from common.evidence_store import FileEvidenceStore  # noqa: E402
from common.models import (  # noqa: E402
    Evidence,
    EvidenceHypothesis,
    EvidenceItem,
    FailureTaxonomy,
    IncidentCreatedEvent,
)
from coordinator.agent import CoordinatorAgent  # noqa: E402
from coordinator.policy import OpaCliGate  # noqa: E402
from coordinator.remediation_gate import Approval, RemediationGate  # noqa: E402
from coordinator.synthesis import IncidentReport  # noqa: E402


REPO = "jenkinsguardians/target-app"
BREAKING_SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
EMPTY_SHA = "c3d4e5f60718293a4b5c6d7e8f90123456789012"
REQUIREMENTS = "target_app/requirements.txt"
WIDTH = 76


# --- output -------------------------------------------------------------------


def banner(title: str, subtitle: str = "") -> None:
    print("\n" + "=" * WIDTH)
    print(title)
    if subtitle:
        print(subtitle)
    print("=" * WIDTH)


def step(label: str, text: str) -> None:
    print(f"\n  [{label}] {text}")


def detail(text: str, *, label: str = "") -> None:
    """Print an indented line, wrapping long text at word boundaries.

    ``label`` is a fixed-width prefix (``"denied    "``) kept on the first line and
    matched by indentation on the rest, so wrapped text stays aligned.
    """
    indent = " " * 8
    if not label and len(text) <= WIDTH - len(indent):
        print(f"{indent}{text}")
        return
    print(
        textwrap.fill(
            text,
            width=WIDTH,
            initial_indent=indent + label,
            subsequent_indent=indent + " " * len(label),
            break_long_words=False,
            break_on_hyphens=False,
        )
    )


class Checks:
    """Expectations for one scenario, printed as they are checked."""

    def __init__(self, scenario: str) -> None:
        self.scenario = scenario
        self.results: list[tuple[str, bool]] = []

    def expect(self, description: str, ok: bool) -> None:
        self.results.append((description, ok))
        print(f"        {'PASS' if ok else 'FAIL'}  {description}")

    @property
    def passed(self) -> bool:
        return all(ok for _, ok in self.results)


# --- an in-memory GitHub ------------------------------------------------------


def _blob_sha(content: str) -> str:
    return hashlib.sha1(content.encode("utf-8")).hexdigest()


class DemoGitHub:
    """Just enough of the GitHub REST API, held in memory, for read and write clients.

    Records every write so the demo can show - and check - exactly what changed.
    """

    def __init__(self, *, sha: str, files: list[dict[str, Any]], tree: dict[str, str], message: str) -> None:
        self.sha = sha
        self.tree = dict(tree)  # the repository as of the commit that broke the build
        self.branches: dict[str, dict[str, str]] = {}
        self.pull_requests: list[dict[str, Any]] = []
        self.writes: list[str] = []
        self.commit_payload = {
            "sha": sha,
            "html_url": f"https://github.com/{REPO}/commit/{sha}",
            "commit": {"message": message, "author": {"name": "Ishita", "date": "2026-10-26T09:30:00Z"}},
            "files": files,
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.split(f"/repos/{REPO}", 1)[-1]
        method = request.method

        if method == "GET" and path == f"/commits/{self.sha}":
            return httpx.Response(200, json=self.commit_payload)
        if method == "GET" and path == f"/commits/{self.sha}/pulls":
            return httpx.Response(200, json=[])
        if method == "GET" and path == "/commits":
            return httpx.Response(200, json=[self.commit_payload])

        if method == "POST" and path == "/git/refs":
            import json

            body = json.loads(request.content)
            name = body["ref"].removeprefix("refs/heads/")
            if name in self.branches:
                return httpx.Response(422, json={"message": "Reference already exists"})
            self.branches[name] = dict(self.tree)
            self.writes.append(f"created branch {name}")
            return httpx.Response(201, json={"ref": body["ref"]})

        if path.startswith("/contents/"):
            file_path = path.removeprefix("/contents/")
            ref = request.url.params.get("ref", "")
            if method == "GET":
                files = self.branches.get(ref, self.tree)
                if file_path not in files:
                    return httpx.Response(404, json={"message": "Not Found"})
                content = files[file_path]
                return httpx.Response(
                    200,
                    json={
                        "encoding": "base64",
                        "content": base64.b64encode(content.encode()).decode(),
                        "sha": _blob_sha(content),
                    },
                )
            if method == "PUT":
                import json

                body = json.loads(request.content)
                files = self.branches[body["branch"]]
                if body["sha"] != _blob_sha(files[file_path]):
                    return httpx.Response(409, json={"message": "does not match sha"})
                files[file_path] = base64.b64decode(body["content"]).decode()
                self.writes.append(f"committed {file_path} on {body['branch']}")
                return httpx.Response(200, json={"commit": {"sha": f"c0ffee{len(self.writes):02d}"}})

        if method == "POST" and path == "/pulls":
            import json

            body = json.loads(request.content)
            number = 100 + len(self.pull_requests)
            self.pull_requests.append({**body, "number": number})
            self.writes.append(f"opened pull request #{number}")
            return httpx.Response(
                201,
                json={"number": number, "html_url": f"https://github.com/{REPO}/pull/{number}", "draft": body["draft"]},
            )

        return httpx.Response(404, json={"message": f"unhandled {method} {path}"})

    def reader(self) -> GitHubClient:
        transport = httpx.MockTransport(self.handler)
        return GitHubClient(REPO, "demo-token", client=httpx.AsyncClient(transport=transport), max_retries=0)

    def writer(self) -> GitHubWriter:
        transport = httpx.MockTransport(self.handler)
        return GitHubWriter(REPO, "demo-token", client=httpx.AsyncClient(transport=transport))


# --- shared setup -------------------------------------------------------------


def find_opa() -> str | None:
    explicit = os.environ.get("OPA_BIN")
    if explicit and Path(explicit).is_file():
        return explicit
    for candidate in (shutil.which("opa"), REPO_ROOT / ".bin" / "opa.exe", REPO_ROOT / ".bin" / "opa"):
        if candidate and Path(candidate).is_file():
            return str(candidate)
    return None


def incident(incident_id: str, sha: str, failed_stage: str) -> IncidentCreatedEvent:
    return IncidentCreatedEvent(
        incident_id=UUID(incident_id),
        job_name="target-app/main",
        build_number=42,
        build_url="http://jenkins:8080/job/target-app/job/main/42/",
        branch="main",
        git_commit=sha,
        failed_stage=failed_stage,
        timestamp=datetime(2026, 10, 26, 9, 31, tzinfo=timezone.utc),
    )


def recorded(
    agent: str,
    incident_id: UUID,
    failure_type: FailureTaxonomy,
    confidence: float,
    summary: str,
    *,
    status: str = "completed",
    finding: str = "",
) -> Evidence:
    """Evidence another agent would have written. Labelled as recorded wherever shown."""
    items: list[EvidenceItem] = []
    hypotheses: list[EvidenceHypothesis] = []
    if finding:
        items = [EvidenceItem(id=f"{agent}-1", kind="log_excerpt", source=agent, content=finding)]
        if failure_type is not FailureTaxonomy.UNKNOWN:
            hypotheses = [
                EvidenceHypothesis(
                    hypothesis=summary,
                    failure_type=failure_type,
                    confidence=confidence,
                    supporting_evidence=[f"{agent}-1"],
                )
            ]
    return Evidence(
        incident_id=incident_id,
        agent=agent,  # type: ignore[arg-type]
        created_at=datetime(2026, 10, 26, 9, 32, tzinfo=timezone.utc),
        status=status,  # type: ignore[arg-type]
        failure_type=failure_type,
        summary=summary,
        confidence=confidence,
        evidence_items=items,
        root_cause_hypotheses=hypotheses,
        redaction_applied=True,
    )


def show_report(report: IncidentReport) -> None:
    detail(f"status      {report.status}")
    detail(f"cause       {report.failure_type.value}")
    detail(f"confidence  {report.confidence:.2f}")
    detail(f"actionable  {report.actionable}")
    detail("")
    detail(f"{'agent':<15}{'verdict':<26}{'conf':<7}status")
    for finding in report.findings:
        detail(f"{finding.agent:<15}{finding.failure_type.value:<26}{finding.confidence:<7.2f}{finding.status}")
    for conflict in report.conflicts:
        detail(f"disagreement: {conflict.describe()}")


def show_plan(plan: FixPlan) -> None:
    detail(f"branch  {plan.branch_name}")
    for edit in plan.edits:
        detail(f"file    {edit.path}")
        detail(f"  - {edit.before}")
        detail(f"  + {edit.after}")
        if edit.reason:
            detail(f"({edit.reason})", label="  ")


class Pipeline:
    """One isolated run: its own blackboard, audit trail, GitHub and Coordinator."""

    def __init__(self, workdir: Path, github: DemoGitHub, opa: str) -> None:
        self.store = FileEvidenceStore(workdir / "evidence")
        self.audit = FileAuditLog(workdir / "audit")
        self.github = github
        self.coordinator = CoordinatorAgent(self.store)
        self.gate = RemediationGate(
            OpaCliGate(opa, str(REPO_ROOT / "policies")), github.writer(), self.audit
        )
        self.notifications: list[str] = []

    async def notify(self, message: Any) -> None:
        """The A2A sink: record the notification, then hand it to the ADK Coordinator."""
        self.notifications.append(dict(message.metadata).get("agent", "?"))
        await self.coordinator.on_evidence_ready(message)

    async def run_code_agent(self, event: IncidentCreatedEvent) -> Evidence:
        agent = CodeInvestigationAgent(
            self.github.reader(), self.store, self.audit, notifier=self.notify
        )
        return await agent.run_event(event)

    async def post_recorded(self, evidence: Evidence) -> None:
        await self.store.write(evidence)
        await self.notify(evidence_ready(evidence.incident_id, evidence.agent))


# --- scenarios ----------------------------------------------------------------


def tc03_github() -> DemoGitHub:
    good = (REPO_ROOT / REQUIREMENTS).read_text(encoding="utf-8")
    broken = good.replace("httpx==0.28.1", "httpx==0.99.0")
    return DemoGitHub(
        sha=BREAKING_SHA,
        message="Upgrade httpx for the new retry API",
        files=[
            {
                "filename": REQUIREMENTS,
                "status": "modified",
                "additions": 1,
                "deletions": 1,
                "patch": "@@ -4,1 +4,1 @@\n-httpx==0.28.1\n+httpx==0.99.0",
            }
        ],
        tree={REQUIREMENTS: broken},
    )


async def investigate_tc03(pipeline: Pipeline, incident_id: str) -> tuple[IncidentReport, Any, Checks]:
    checks = Checks("TC-03")
    event = incident(incident_id, BREAKING_SHA, "Setup")

    step("1", "Jenkins build #42 fails at Setup; the incident is announced to every agent")
    detail(f"incident  {event.incident_id}")
    detail(f"commit    {BREAKING_SHA[:12]}  'Upgrade httpx for the new retry API'")

    step("2", "Code agent (CrewAI) investigates the commit on GitHub")
    code = await pipeline.run_code_agent(event)
    detail(f"verdict   {code.failure_type.value} at {code.confidence:.2f}")
    if code.root_cause_hypotheses:
        top = code.root_cause_hypotheses[0]
        detail(f"cites     {', '.join(top.supporting_evidence)}")
    checks.expect("code agent names a dependency regression at >= 0.70",
                  code.failure_type is FailureTaxonomy.DEPENDENCY_REGRESSION and code.confidence >= 0.7)

    step("3", "Jenkins and metrics agents report (RECORDED evidence - see docstring)")
    await pipeline.post_recorded(recorded(
        "jenkins_agent", event.incident_id, FailureTaxonomy.DEPENDENCY_REGRESSION, 0.65,
        "pip could not resolve httpx==0.99.0 during Setup",
        finding="ERROR: No matching distribution found for httpx==0.99.0",
    ))
    await pipeline.post_recorded(recorded(
        "metrics_agent", event.incident_id, FailureTaxonomy.UNKNOWN, 0.2,
        "Metrics do not explain this failure: 4 metric(s) checked, none anomalous.",
        status="insufficient_evidence",
    ))
    detail(f"A2A evidence.ready received from: {', '.join(pipeline.notifications)}")
    view = await pipeline.coordinator.collect(event.incident_id)
    checks.expect("all three agents reached the Coordinator over A2A", view.complete)

    step("4", "Coordinator (Google ADK) fuses the blackboard into one report")
    report = await pipeline.coordinator.synthesize(event.incident_id)
    assert report is not None
    show_report(report)
    checks.expect("report: resolved dependency regression, actionable", report.actionable
                  and report.failure_type is FailureTaxonomy.DEPENDENCY_REGRESSION)
    checks.expect("fused confidence never claims certainty (<= 0.95)", report.confidence <= 0.95)
    checks.expect("the metrics agent's abstention is recorded, not counted as a vote",
                  "metrics_agent" in report.abstaining_agents)

    step("5", "Remediation engine proposes a minimal fix")
    _, files = await pipeline.github.reader().get_commit(BREAKING_SHA)
    plan = propose_fix(report, files, base_ref=BREAKING_SHA)
    if isinstance(plan, FixPlan):
        show_plan(plan)
    checks.expect("fix reverts exactly httpx==0.99.0 -> httpx==0.28.1",
                  isinstance(plan, FixPlan) and len(plan.edits) == 1
                  and plan.edits[0].before == "httpx==0.99.0" and plan.edits[0].after == "httpx==0.28.1")
    return report, plan, checks


async def scenario_tc03(workdir: Path, opa: str) -> Checks:
    banner("TC-03  Dependency regression - the full path to a draft pull request",
           "A commit bumps httpx to a version that does not exist; Setup fails.")
    github = tc03_github()
    pipeline = Pipeline(workdir, github, opa)
    _, plan, checks = await investigate_tc03(pipeline, "11111111-0303-4303-8303-000000000003")
    if not isinstance(plan, FixPlan):
        return checks

    step("6", "Gate 1 - OPA remediation policy (real Rego, before any human is asked)")
    submitted = await pipeline.gate.submit(plan)
    detail(f"status    {submitted.status}")
    for warning in submitted.warnings:
        detail(warning, label="warning   ")
    checks.expect("policy allows the fix and asks a human", submitted.status == "awaiting_approval")
    checks.expect("nothing has touched GitHub yet", github.writes == [])

    step("7", "Gate 2 - a named human approves")
    approval = Approval(decision="approved", reviewer="ishita", reason="Pin back; 0.99.0 does not exist.")
    detail(f"{approval.reviewer}: {approval.decision} - {approval.reason}")

    step("8", "Gate 3 - OPA pull-request policy, then branch + DRAFT pull request")
    final = await pipeline.gate.decide(plan, submitted, approval)
    detail(f"status    {final.status}")
    for write in github.writes:
        detail(f"github    {write}")
    pr = github.pull_requests[0] if github.pull_requests else {}
    checks.expect("a draft PR was opened, never a ready one", final.status == "pr_created" and pr.get("draft") is True)
    branch_file = github.branches.get(plan.branch_name, {}).get(REQUIREMENTS, "")
    original = github.tree[REQUIREMENTS]
    changed = [(a, b) for a, b in zip(original.splitlines(), branch_file.splitlines()) if a != b]
    checks.expect("on the branch, only the httpx line changed", changed == [("httpx==0.99.0", "httpx==0.28.1")])
    checks.expect("the base commit's files were not modified", "httpx==0.99.0" in github.tree[REQUIREMENTS])

    step("9", "Audit trail for this incident")
    for record in await pipeline.audit.read(plan.incident_id):
        if record.actor == "coordinator":
            summary = textwrap.shorten(record.summary, width=42, placeholder="...")
            detail(f"{record.event_type:<22} ok={record.ok!s:<5} {summary}")
    return checks


async def scenario_tc15(workdir: Path, opa: str) -> Checks:
    banner("TC-15  Unsafe fix - the policy blocks it before anyone sees it",
           "The obvious 'fix' for a failing test: skip the test. Everything goes green; the bug ships.")
    checks = Checks("TC-15")
    github = tc03_github()
    pipeline = Pipeline(workdir, github, opa)
    unsafe = FixPlan(
        incident_id=UUID("11111111-1515-4515-8515-000000000015"),
        failure_type=FailureTaxonomy.CODE_TEST_FAILURE,
        confidence=0.9,
        base_ref=BREAKING_SHA,
        branch_name="remediation/11111111-code_test_failure-a0",
        commit_message="Make the build green",
        rationale="Skip the failing test.",
        edits=(FileEdit(path="target_app/tests/test_orders.py",
                        before="def test_orders():", after="@pytest.mark.skip(reason='flaky')"),),
    )
    step("1", "A proposal arrives that adds a skip marker to a test")
    detail("+ @pytest.mark.skip(reason='flaky')")

    step("2", "Gate 1 - OPA remediation policy (real Rego)")
    record = await pipeline.gate.submit(unsafe)
    detail(f"status    {record.status}")
    for violation in record.violations:
        detail(violation, label="denied    ")
    checks.expect("the policy rejects it", record.status == "policy_rejected")
    checks.expect("the rejection names the skipped test",
                  any("disables a test" in v for v in record.violations))
    checks.expect("no human was asked and nothing touched GitHub", github.writes == [])
    return checks


async def scenario_tc12(workdir: Path, opa: str) -> Checks:
    banner("TC-12  No code change - the system finds the cause and refuses to touch the code",
           "Latency spikes and the build times out, but the commit changed nothing.")
    checks = Checks("TC-12")
    github = DemoGitHub(sha=EMPTY_SHA, message="Merge branch 'main'", files=[], tree={})
    pipeline = Pipeline(workdir, github, opa)
    event = incident("11111111-1212-4212-8212-000000000012", EMPTY_SHA, "Test")

    step("1", "Code agent investigates a commit that changed no files")
    code = await pipeline.run_code_agent(event)
    detail(f"verdict   {code.failure_type.value} at {code.confidence:.2f}  ({code.status})")
    checks.expect("code agent does not blame the commit", code.failure_type is FailureTaxonomy.UNKNOWN
                  and code.status == "insufficient_evidence")

    step("2", "Jenkins and metrics agents report (RECORDED evidence)")
    await pipeline.post_recorded(recorded(
        "jenkins_agent", event.incident_id, FailureTaxonomy.TIMEOUT, 0.85,
        "A timeout signal occurred in the failed Test stage.",
        finding="httpx.ReadTimeout: GET /orders exceeded 2.0s",
    ))
    await pipeline.post_recorded(recorded(
        "metrics_agent", event.incident_id, FailureTaxonomy.TIMEOUT, 0.9,
        "latency_p95: peak 2.4s vs baseline median 0.12s",
        finding="p95 latency 2.4s, baseline 0.12s (z=19.0)",
    ))

    step("3", "Coordinator fuses the evidence")
    report = await pipeline.coordinator.synthesize(event.incident_id)
    assert report is not None
    show_report(report)
    checks.expect("the cause is found: a timeout, from two agents agreeing",
                  report.failure_type is FailureTaxonomy.TIMEOUT and set(report.agreeing_agents)
                  == {"jenkins_agent", "metrics_agent"})

    step("4", "Remediation engine is asked for a fix")
    result = propose_fix(report, [], base_ref=EMPTY_SHA)
    if isinstance(result, NoFix):
        detail(result.reason, label="no fix    ")
    checks.expect("it declines: a timeout has no automated code fix", isinstance(result, NoFix))
    checks.expect("nothing touched GitHub", github.writes == [])
    return checks


async def scenario_tc16(workdir: Path, opa: str) -> Checks:
    banner("TC-16  A human rejects the fix - nothing is merged or deployed",
           "Same incident as TC-03; this time the reviewer says no.")
    github = tc03_github()
    pipeline = Pipeline(workdir, github, opa)
    _, plan, checks = await investigate_tc03(pipeline, "11111111-1616-4616-8616-000000000016")
    checks.scenario = "TC-16"
    if not isinstance(plan, FixPlan):
        return checks

    step("6", "Gate 1 passes; a reviewer is asked")
    submitted = await pipeline.gate.submit(plan)
    detail(f"status    {submitted.status}")

    step("7", "Gate 2 - the reviewer rejects")
    rejection = Approval(decision="rejected", reviewer="shreya", reason="Pin to 0.28.1 is outdated; I'll pick a version.")
    final = await pipeline.gate.decide(plan, submitted, rejection)
    detail(f"status    {final.status}")
    detail(final.detail, label="reason    ")
    checks.expect("the proposal is rejected", final.status == "rejected")
    checks.expect("no branch and no pull request were created", github.writes == [])
    decided = [r for r in await pipeline.audit.read(plan.incident_id) if r.event_type == "approval_decided"]
    checks.expect("the rejection and its reviewer are in the audit trail",
                  len(decided) == 1 and decided[0].payload.get("reviewer") == "shreya")
    return checks


# --- entry point --------------------------------------------------------------


async def run(opa: str) -> list[Checks]:
    scenarios: list[Callable[[Path, str], Any]] = [scenario_tc03, scenario_tc15, scenario_tc12, scenario_tc16]
    results: list[Checks] = []
    for scenario in scenarios:
        with tempfile.TemporaryDirectory() as tmp:
            results.append(await scenario(Path(tmp), opa))
    return results


def main() -> int:
    opa = find_opa()
    if opa is None:
        print("opa not found. Run:  python scripts/get_opa.py", file=sys.stderr)
        print("The demo uses the real Rego policies and will not fake a policy decision.", file=sys.stderr)
        return 2

    banner("JenkinsGuardians - P3 pipeline demo",
           "CrewAI code agent -> A2A -> Google ADK Coordinator -> fix -> OPA -> human -> draft PR")
    print(f"  policy engine: {opa}")
    print("  GitHub: in-memory fake | Jenkins & metrics evidence: recorded | everything else: real code")

    results = asyncio.run(run(opa))

    banner("Summary")
    for checks in results:
        passed = sum(ok for _, ok in checks.results)
        print(f"  {checks.scenario:<7} {'PASS' if checks.passed else 'FAIL'}   {passed}/{len(checks.results)} expectations met")
    all_passed = all(checks.passed for checks in results)
    print("\n  " + ("All scenarios behaved as expected." if all_passed else "SOME EXPECTATIONS FAILED - see above."))
    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

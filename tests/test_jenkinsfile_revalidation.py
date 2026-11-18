"""target_app/Jenkinsfile: a build of a fix branch is a re-validation, reported to the validation endpoint.

Found live (2026-11-18): the PR for an approved fix was built by Jenkins's branch scan with no INCIDENT_ID, so the success
branch never reported, and the failure branch opened a brand-new incident for the fix itself. The backend already resolves the
incident from the branch name; only the Jenkinsfile was not calling it.
"""

from pathlib import Path
import re

from common.fix_branch import PREFIX, format_fix_branch, parse_fix_branch

JENKINSFILE = (Path(__file__).resolve().parents[1] / "target_app" / "Jenkinsfile").read_text(encoding="utf-8")


def block(name: str) -> str:
    """The body of a `post { name { ... } }` block (up to the next block at the same indent)."""
    match = re.search(rf"\t\t{name} \{{\n(.*?)\n\t\t\}}", JENKINSFILE, re.DOTALL)
    assert match, f"no {name} block"
    return match.group(1)


def test_the_helper_recognises_the_branch_the_remediation_engine_creates() -> None:
    helper = re.search(r"def isRemediationBuild\(\) \{(.*?)\n\}", JENKINSFILE, re.DOTALL).group(1)
    assert f"startsWith('{PREFIX}')" in helper
    assert "params.INCIDENT_ID" in helper  # an explicit trigger still works
    assert parse_fix_branch(format_fix_branch("b4f34d05-1183-414b-8fad-eb8c32e90ce3", 0)) is not None


def test_a_passing_fix_build_is_reported_as_a_successful_re_validation() -> None:
    success = block("success")
    assert "isRemediationBuild()" in success and "/webhooks/jenkins/validation', 'SUCCESS'" in success


def test_a_failing_fix_build_is_a_failed_re_validation_not_a_new_incident() -> None:
    failure = block("failure")
    assert "isRemediationBuild()" in failure
    assert "/webhooks/jenkins/validation', 'FAILURE'" in failure
    assert "postAgentWebhook('/webhooks/jenkins')" in failure  # an ordinary branch still opens an incident


def test_the_parameters_stay_declared_so_an_explicit_trigger_keeps_working() -> None:
    assert "name: 'INCIDENT_ID'" in JENKINSFILE and "name: 'REMEDIATION_ATTEMPT'" in JENKINSFILE

# Unit tests for the remediation guardrails.
#
#   docker run --rm -v "$PWD:/w" openpolicyagent/opa:1.21.1-static test /w/policies -v
#
# Owner: P3. Every deny rule gets a case that trips it and the happy-path proposal is
# asserted to stay allowed, so tightening one rule cannot quietly block everything.

package jenkinsguardians.remediation_test

import data.jenkinsguardians.remediation
import rego.v1

minimal_fix := {
	"incident_id": "11111111-2222-3333-4444-555555555555",
	"failure_type": "config_error",
	"confidence": 0.82,
	"attempt": 0,
	"changed_files": [{
		"path": "target_app/config/settings.yaml",
		"status": "modified",
		"additions": 1,
		"deletions": 1,
		"patch": "@@ -1,2 +1,2 @@\n-database_timeout_seconds: -5\n+database_timeout_seconds: 3",
	}],
}

with_file(file) := object.union(minimal_fix, {"changed_files": [file]})

test_minimal_fix_is_allowed if {
	remediation.allow with input as minimal_fix
	count(remediation.deny) == 0 with input as minimal_fix
}

test_empty_proposal_is_denied if {
	not remediation.allow with input as object.union(minimal_fix, {"changed_files": []})
}

test_low_confidence_is_denied if {
	not remediation.allow with input as object.union(minimal_fix, {"confidence": 0.3})
}

test_too_many_files_is_denied if {
	files := [object.union(minimal_fix.changed_files[0], {"path": sprintf("target_app/app/m%v.py", [i])}) |
		some i in numbers.range(1, 6)
	]
	not remediation.allow with input as object.union(minimal_fix, {"changed_files": files})
}

test_oversized_diff_is_denied if {
	big := object.union(minimal_fix.changed_files[0], {"additions": 60, "deletions": 20})
	not remediation.allow with input as with_file(big)
}

test_touching_the_pipeline_is_denied if {
	file := object.union(minimal_fix.changed_files[0], {"path": "target_app/Jenkinsfile"})
	not remediation.allow with input as with_file(file)
}

test_touching_policies_is_denied if {
	file := object.union(minimal_fix.changed_files[0], {"path": "policies/remediation.rego"})
	not remediation.allow with input as with_file(file)
}

test_touching_secrets_is_denied if {
	file := object.union(minimal_fix.changed_files[0], {"path": "target_app/.env"})
	not remediation.allow with input as with_file(file)
}

test_outside_the_target_application_is_denied if {
	file := object.union(minimal_fix.changed_files[0], {"path": "backend/main.py"})
	not remediation.allow with input as with_file(file)
}

test_deleting_a_test_is_denied if {
	file := {
		"path": "target_app/tests/test_orders.py",
		"status": "removed",
		"additions": 0,
		"deletions": 12,
		"patch": "",
	}
	not remediation.allow with input as with_file(file)
}

test_skipping_a_test_is_denied if {
	file := object.union(minimal_fix.changed_files[0], {
		"path": "target_app/tests/test_orders.py",
		"patch": "@@ -1,3 +1,4 @@\n+@pytest.mark.skip(reason=\"flaky\")\n def test_orders():",
	})
	not remediation.allow with input as with_file(file)
}

test_disabling_tls_verification_is_denied if {
	file := object.union(minimal_fix.changed_files[0], {
		"path": "target_app/app/db_client.py",
		"patch": "@@ -4,1 +4,1 @@\n-client = httpx.Client()\n+client = httpx.Client(verify=False)",
	})
	not remediation.allow with input as with_file(file)
}

test_removed_lines_are_not_scanned_as_additions if {
	# A diff that *removes* a skip marker must stay allowed.
	file := object.union(minimal_fix.changed_files[0], {
		"path": "target_app/tests/test_orders.py",
		"patch": "@@ -1,4 +1,3 @@\n-@pytest.mark.skip(reason=\"flaky\")\n def test_orders():",
	})
	remediation.allow with input as with_file(file)
}

test_dependency_change_warns_but_is_allowed if {
	file := object.union(minimal_fix.changed_files[0], {
		"path": "target_app/requirements.txt",
		"patch": "@@ -5,1 +5,1 @@\n-httpx==0.99.0\n+httpx==0.28.1",
	})
	remediation.allow with input as with_file(file)
	count(remediation.warn) > 0 with input as with_file(file)
}

test_retry_attempt_warns if {
	count(remediation.warn) > 0 with input as object.union(minimal_fix, {"attempt": 1})
}

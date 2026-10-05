# Unit tests for the pull-request gate.
#
# Owner: P3. The point of this gate is that it cannot be satisfied by the agent alone,
# so most of these tests assert that something is refused.

package jenkinsguardians.pullrequest_test

import data.jenkinsguardians.pullrequest
import rego.v1

approved := {
	"policy_allowed": true,
	"draft": true,
	"auto_merge": false,
	"approval": {"decision": "approved", "reviewer": "ishita"},
}

test_approved_draft_is_allowed if {
	pullrequest.allow with input as approved
}

test_missing_approval_is_denied if {
	# Built literally, not via object.union: object.union merges recursively, so
	# unioning an empty approval back onto `approved` would leave it untouched.
	not pullrequest.allow with input as {"policy_allowed": true, "draft": true, "auto_merge": false}
}

test_blank_approval_is_denied if {
	not pullrequest.allow with input as object.remove(approved, {"approval"})
}

test_rejected_approval_is_denied if {
	rejected := {"decision": "rejected", "reviewer": "ishita"}
	not pullrequest.allow with input as object.union(approved, {"approval": rejected})
}

test_changes_requested_is_denied if {
	requested := {"decision": "changes_requested", "reviewer": "shreya"}
	not pullrequest.allow with input as object.union(approved, {"approval": requested})
}

test_unattributed_approval_is_denied if {
	anonymous := {"decision": "approved", "reviewer": ""}
	not pullrequest.allow with input as object.union(approved, {"approval": anonymous})
}

test_policy_rejection_is_denied if {
	not pullrequest.allow with input as object.union(approved, {"policy_allowed": false})
}

test_non_draft_is_denied if {
	not pullrequest.allow with input as object.union(approved, {"draft": false})
}

test_auto_merge_is_denied if {
	not pullrequest.allow with input as object.union(approved, {"auto_merge": true})
}

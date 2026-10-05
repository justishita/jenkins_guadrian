# The gate between an approved remediation and a pull request existing.
#
# Owner: P3. `remediation.rego` decides whether a proposal is safe to show a human;
# this decides whether a pull request may be opened at all. Both must pass, and this
# one cannot pass without a recorded human decision.

package jenkinsguardians.pullrequest

import rego.v1

default allow := false

deny contains msg if {
	not object.get(input, "policy_allowed", false)
	msg := "remediation policy did not allow this proposal; no pull request may be created"
}

deny contains msg if {
	object.get(input, "approval", {}) == {}
	msg := "no human approval is recorded; a pull request requires an explicit decision"
}

deny contains msg if {
	decision := object.get(object.get(input, "approval", {}), "decision", "")
	decision != ""
	decision != "approved"
	msg := sprintf("the reviewer recorded %v, not an approval; no pull request may be created", [decision])
}

deny contains msg if {
	approval := object.get(input, "approval", {})
	approval != {}
	object.get(approval, "reviewer", "") == ""
	msg := "the approval does not name a reviewer; the audit trail must attribute the decision"
}

deny contains msg if {
	not object.get(input, "draft", false)
	msg := "agent-generated pull requests must be opened as drafts"
}

deny contains msg if {
	object.get(input, "auto_merge", false)
	msg := "auto-merge must not be enabled on an agent-generated pull request"
}

allow if count(deny) == 0

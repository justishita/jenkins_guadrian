# Guardrails for a remediation the Code & Remediation agent proposes.
#
# Owner: P3. Evaluated before any change reaches a human reviewer, so the reviewer
# never spends attention on a proposal that was never allowed to exist. A `deny` is
# a hard stop; a `warn` still reaches the reviewer but says what to look at.
#
#   opa eval -d policies -I 'data.jenkinsguardians.remediation' < proposal.json
#   conftest test --policy policies --namespace jenkinsguardians.remediation proposal.json

package jenkinsguardians.remediation

import rego.v1

default allow := false

# A fix proposed from weak evidence is worse than no fix (TC-14): below this, the
# agent must keep investigating rather than guess.
min_confidence := 0.5

# "Minimal fix" is a project requirement, not a style preference. A sprawling diff is
# a rewrite and belongs to a human.
max_changed_files := 5

max_changed_lines := 40

# The agent only ever repairs the application under test. Everything that defines how
# the pipeline runs, what it checks, or who it trusts is off limits to automation.
allowed_root_patterns := ["target_app/**"]

protected_path_patterns := [
	"Jenkinsfile",
	"**/Jenkinsfile",
	".github/**",
	"policies/**",
	"jenkins/**",
	"monitoring/**",
	"migrations/**",
	"docker-compose.yml",
	"**/Dockerfile",
	"Makefile",
	"alembic.ini",
]

secret_path_patterns := [
	".env",
	"**/.env",
	"**/.env.*",
	"**/*.pem",
	"**/*.key",
	"**/*.p12",
	"**/id_rsa*",
	"**/secrets*",
	"**/credentials*",
]

# Added lines that silence a test or a safety gate rather than fix the cause.
disabling_line_patterns := [
	`@pytest\.mark\.skip`,
	`@pytest\.mark\.xfail`,
	`@unittest\.skip`,
	`pytest\.skip\(`,
	`--no-verify`,
	`--exitfirst`,
	`(?i)#\s*noqa\s*$`,
	`(?i)#\s*type:\s*ignore`,
	`(?i)verify\s*=\s*False`,
	`(?i)ssl[_-]?verify\s*[:=]\s*(false|no|0)`,
	`(?i)(skip|disable|ignore)[_-]?(tests|security|scan|lint|checks)\s*[:=]\s*(true|yes|1)`,
	`(?i)continue-on-error\s*:\s*true`,
]

changed_files := object.get(input, "changed_files", [])

confidence := object.get(input, "confidence", 0)

added_lines contains entry if {
	some file in changed_files
	some line in split(object.get(file, "patch", ""), "\n")
	startswith(line, "+")
	not startswith(line, "+++")
	entry := {"path": object.get(file, "path", "<unknown>"), "line": trim_space(line)}
}

total_changed_lines := sum([n |
	some file in changed_files
	n := object.get(file, "additions", 0) + object.get(file, "deletions", 0)
])

is_test_path(path) if regex.match(`(^|/)tests?/|(^|/)test_[^/]*\.py$|_test\.py$`, path)

is_dependency_manifest(path) if {
	some name in [
		"requirements.txt",
		"requirements-dev.txt",
		"pyproject.toml",
		"poetry.lock",
		"setup.py",
		"setup.cfg",
		"Pipfile",
		"Pipfile.lock",
		"package.json",
		"package-lock.json",
	]
	endswith(path, name)
}

matches_any(patterns, path) if {
	some pattern in patterns
	glob.match(pattern, ["/"], path)
}

# --- Hard stops ---------------------------------------------------------------

deny contains msg if {
	count(changed_files) == 0
	msg := "proposal changes no files; there is nothing to review or validate"
}

deny contains msg if {
	confidence < min_confidence
	msg := sprintf(
		"confidence %v is below the required %v; the agent must gather more evidence instead of proposing a fix",
		[confidence, min_confidence],
	)
}

deny contains msg if {
	count(changed_files) > max_changed_files
	msg := sprintf(
		"proposal touches %v files, above the %v allowed for a minimal fix",
		[count(changed_files), max_changed_files],
	)
}

deny contains msg if {
	total_changed_lines > max_changed_lines
	msg := sprintf(
		"proposal changes %v lines, above the %v allowed for a minimal fix",
		[total_changed_lines, max_changed_lines],
	)
}

deny contains msg if {
	some file in changed_files
	path := object.get(file, "path", "<unknown>")
	matches_any(secret_path_patterns, path)
	msg := sprintf("proposal modifies the secret-bearing path %v; credentials are never changed by an agent", [path])
}

deny contains msg if {
	some file in changed_files
	path := object.get(file, "path", "<unknown>")
	matches_any(protected_path_patterns, path)
	msg := sprintf("proposal modifies the protected pipeline/policy path %v; this needs a human change", [path])
}

deny contains msg if {
	some file in changed_files
	path := object.get(file, "path", "<unknown>")
	not matches_any(allowed_root_patterns, path)
	not matches_any(protected_path_patterns, path)
	not matches_any(secret_path_patterns, path)
	msg := sprintf("proposal modifies %v, which is outside the application the agent may repair", [path])
}

deny contains msg if {
	some file in changed_files
	object.get(file, "status", "") == "removed"
	path := object.get(file, "path", "<unknown>")
	is_test_path(path)
	msg := sprintf("proposal deletes the test file %v; a failing test is evidence, not an obstacle", [path])
}

deny contains msg if {
	some entry in added_lines
	some pattern in disabling_line_patterns
	regex.match(pattern, entry.line)
	msg := sprintf("proposal disables a test or safety check in %v: %v", [entry.path, entry.line])
}

# --- Advisory -----------------------------------------------------------------

warn contains msg if {
	some file in changed_files
	path := object.get(file, "path", "<unknown>")
	is_dependency_manifest(path)
	msg := sprintf("proposal changes the dependency manifest %v; confirm the pinned version is the one that last built green", [path])
}

warn contains msg if {
	confidence < 0.7
	confidence >= min_confidence
	msg := sprintf("confidence %v is moderate; review the cited evidence before approving", [confidence])
}

warn contains msg if {
	object.get(input, "attempt", 0) > 0
	msg := sprintf("this is remediation attempt %v; the previous fix did not validate", [object.get(input, "attempt", 0)])
}

allow if count(deny) == 0

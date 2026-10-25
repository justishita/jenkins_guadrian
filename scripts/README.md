# Scripts

**Owner:** Shared. Place local development, maintenance, and fault-injection scripts here; each script should document its owner and effects.

## Collect Jenkins fixtures

After a failed build, run `python scripts/collect_fixtures.py <job> <build-number>`.
The script loads those settings from the repository `.env` file (or existing process
environment): `JENKINS_URL`, `JENKINS_USER`, and `JENKINS_API_TOKEN`. It writes
redacted console and available JUnit XML files under `tests/jenkins_agent/fixtures/`.
Use `--output-dir` to choose another destination.

## Inject a Jenkins failure

From a clean, non-`main` branch, run
`python scripts/inject_fault.py <scenario>` with one of
`tc01_unit_test`, `tc02_compile_syntax`, or `tc02_compile_import`. The script
creates `fault/<scenario>-<UTC timestamp>`, applies the target-app fault,
writes `scenarios/<scenario>.json` ground truth, commits the two files, pushes
to `origin`, and logs the Jenkins multibranch build URL. `JENKINS_URL` controls
the URL printed (defaults to `http://localhost:8080`).

To delete a pushed and local injection branch, check out any other branch and
run `python scripts/inject_fault.py revert fault/<scenario>-<UTC timestamp>`.
Fault creation refuses to run from `main`, detached HEAD, or a dirty tree.

## P3 pipeline demo

`python scripts/get_opa.py` downloads the pinned, checksum-verified `opa` binary into
`.bin/` (git-ignored). `conftest.py` and the demo find it there, so the real-policy
tests run instead of silently skipping.

`python scripts/demo_p3.py` runs the P3 pipeline end to end with no Docker, Jenkins,
network or API key: the CrewAI code agent, A2A, the Google ADK Coordinator, synthesis,
remediation and the real OPA policies. GitHub is an in-memory fake and the Jenkins and
metrics agents' evidence is recorded. It exits non-zero if any expectation fails. See
`docs/p3_demo.md`.

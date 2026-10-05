# Scripts

**Owner:** Shared. Place local development, maintenance, and fault-injection scripts here; each script should document its owner and effects.

## Collect Jenkins fixtures

After a failed build, run `python scripts/collect_fixtures.py <job> <build-number>`.
The script loads those settings from the repository `.env` file (or existing process
environment): `JENKINS_URL`, `JENKINS_USER`, and `JENKINS_API_TOKEN`. It writes
redacted console and available JUnit XML files under `tests/jenkins_agent/fixtures/`.
Use `--output-dir` to choose another destination.
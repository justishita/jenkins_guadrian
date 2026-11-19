"""scripts/preflight.py must FAIL on each way this stack failed silently, and pass on a healthy one."""

import json
import runpy
from pathlib import Path

import pytest

P = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts" / "preflight.py"))
Result, PASS, WARN, FAIL, SKIP = P["Result"], P["PASS"], P["WARN"], P["FAIL"], P["SKIP"]
LLM_PROBE, MCP_PROBE, PROMETHEUS_PROBE, GITHUB_PROBE = P["LLM_PROBE"], P["MCP_PROBE"], P["PROMETHEUS_PROBE"], P["GITHUB_PROBE"]
JENKINS_REQUIRED, JENKINS_FORBIDDEN, REMEDIATION_NEEDS = P["JENKINS_REQUIRED"], P["JENKINS_FORBIDDEN"], P["REMEDIATION_NEEDS"]


def sh_lines(present: set[str], names: list[str]) -> str:
    return "\n".join(("SET " if n in present else "UNSET ") + n for n in names)


@pytest.fixture(autouse=True)
def no_docker_gid_in_the_environment(monkeypatch):
    """The shell-variable check reads the real environment; the tests must not depend on whoever runs them."""
    monkeypatch.delenv("DOCKER_GID", raising=False)


def test_a_docker_gid_set_in_the_shell_is_a_warning_because_it_overrides_env(monkeypatch) -> None:
    monkeypatch.setenv("DOCKER_GID", "999")
    result = run(FakeStack())["shell DOCKER_GID"]
    assert result.status == WARN and ".env" in result.detail


class FakeStack:
    """A healthy stack by default; each test breaks one thing."""

    def __init__(self) -> None:
        self.rows = [{"Service": s, "State": "running", "Health": "healthy"} for s in ("jenkins", "coordinator", "agent-api")]
        self.jenkins_present = set(JENKINS_REQUIRED)
        self.docker = (0, "29.5.2")
        self.build = {"number": 24, "result": "SUCCESS", "building": False}
        self.build_error: Exception | None = None
        self.llm = {"key_set": True, "offline": False, "model": "gemini-3.1-flash-lite", "status": 200}
        self.mcp = {"jenkins": {"url": "http://mcp-jenkins:8081/mcp", "status": 200}, "analytics": {"url": "http://mcp-analytics:8082/mcp", "status": 200}}
        self.prom = {"url": "http://prometheus:9090", "status": 200, "value": "1"}
        self.remediation_present = set(REMEDIATION_NEEDS)
        self.remediation_extra = "REMEDIATION_ENABLED=1"
        self.github = {"repo_set": True, "token_set": True, "status": 200, "push": True}

    def ps(self):
        return self.rows

    def exec_sh(self, service, script, timeout=120):
        if service == "jenkins" and "docker version" in script:
            return self.docker
        if service == "jenkins":
            return 0, sh_lines(self.jenkins_present, JENKINS_REQUIRED + JENKINS_FORBIDDEN)
        if service == "coordinator":
            return 0, sh_lines(self.remediation_present, REMEDIATION_NEEDS) + "\n" + self.remediation_extra
        raise AssertionError(service)

    def exec_py(self, service, source, timeout=120):
        return {id(LLM_PROBE): self.llm, id(MCP_PROBE): self.mcp, id(PROMETHEUS_PROBE): self.prom, id(GITHUB_PROBE): self.github}[id(source)]

    def jenkins_request(self, method, path):
        if self.build_error:
            raise self.build_error
        return 200, {"lastBuild": self.build}


def run(stack: FakeStack) -> dict[str, Result]:
    results = P["run_all"](stack)
    return {r.name: r for r in results}


def test_a_healthy_stack_has_no_failures_and_no_warnings() -> None:
    results = run(FakeStack())
    assert [r.name for r in results.values() if r.status in (FAIL, WARN)] == []
    assert len(results) >= 11


def test_the_llm_model_being_retired_is_a_failure_with_the_fix_in_the_hint() -> None:
    """HTTP 404 'no longer available to new users': the agents silently fell back to rules for days."""
    stack = FakeStack()
    stack.llm = {"key_set": True, "offline": False, "model": "gemini-2.5-flash", "status": 404, "message": "no longer available to new users"}
    result = run(stack)["LLM"]
    assert result.status == FAIL and "gemini-2.5-flash" in result.detail and "GEMINI_MODEL" in result.hint


@pytest.mark.parametrize(("status", "expected"), [(429, WARN), (503, WARN), (0, WARN), (401, FAIL), (403, FAIL)])
def test_other_llm_answers_are_graded_by_whether_they_are_temporary(status, expected) -> None:
    stack = FakeStack()
    stack.llm = {**stack.llm, "status": status, "message": "x"}
    assert run(stack)["LLM"].status == expected


def test_llm_offline_or_unchecked_is_not_a_pass() -> None:
    stack = FakeStack()
    stack.llm = {"key_set": True, "offline": True, "model": "m"}
    assert run(stack)["LLM"].status == WARN
    stack.llm = {"key_set": True, "offline": False, "model": "m"}  # --skip-llm: no status field
    assert run(stack)["LLM"].status == SKIP
    stack.llm = {"key_set": False, "offline": False, "model": "m"}
    assert run(stack)["LLM"].status == FAIL


def test_agents_using_different_models_are_flagged() -> None:
    stack = FakeStack()
    calls = iter([{**stack.llm, "model": "a"}, {**stack.llm, "model": "b"}, {**stack.llm, "model": "a"}])
    stack.exec_py = lambda service, source, timeout=120: next(calls) if source is LLM_PROBE else FakeStack.exec_py(stack, service, source)
    assert run(stack)["LLM model (agents agree)"].status == WARN


def test_mcp_refusing_the_host_name_is_a_failure_that_names_the_cause() -> None:
    """HTTP 421 Invalid Host header: every tool call failed and the LLM invented the diagnosis."""
    stack = FakeStack()
    stack.mcp["jenkins"] = {"url": "http://mcp-jenkins:8081/mcp", "status": 421, "body": "Invalid Host header"}
    result = run(stack)["MCP jenkins"]
    assert result.status == FAIL and "421" in result.detail and "_transport_security" in result.hint
    assert run(stack)["MCP analytics"].status == PASS


def test_jenkins_seeing_other_services_secrets_is_a_failure() -> None:
    stack = FakeStack()
    stack.jenkins_present |= {"DATABASE_URL", "GEMINI_API_KEY"}
    result = run(stack)["jenkins environment (secrets)"]
    assert result.status == FAIL and "DATABASE_URL" in result.detail and "GEMINI_API_KEY" in result.detail
    assert "env_file" in result.hint


def test_jenkins_missing_a_variable_its_config_reads_is_a_failure() -> None:
    stack = FakeStack()
    stack.jenkins_present -= {"GITHUB_REPO_OWNER"}
    result = run(stack)["jenkins environment (config)"]
    assert result.status == FAIL and "GITHUB_REPO_OWNER" in result.detail


def test_docker_socket_permission_denied_is_a_failure_that_names_docker_gid() -> None:
    stack = FakeStack()
    stack.docker = (1, "permission denied while trying to connect to the Docker daemon socket")
    result = run(stack)["jenkins -> docker socket"]
    assert result.status == FAIL and "DOCKER_GID" in result.hint


def test_a_failing_build_of_main_is_a_failure_and_a_missing_one_is_only_a_warning() -> None:
    stack = FakeStack()
    stack.build = {"number": 23, "result": "FAILURE", "building": False}
    assert run(stack)["jenkins build of main"].status == FAIL
    stack.build = {}
    assert run(stack)["jenkins build of main"].status == WARN
    stack.build = {"number": 5, "building": True}
    assert run(stack)["jenkins build of main"].status == WARN


def test_no_jenkins_credentials_skips_that_check_instead_of_failing() -> None:
    stack = FakeStack()
    stack.build_error = RuntimeError("no Jenkins address or credentials")
    assert run(stack)["jenkins build of main"].status == SKIP


def test_a_target_that_is_down_is_a_failure() -> None:
    """Prometheus scraped a host that did not exist, so the Metrics agent said 'target down' for every incident."""
    stack = FakeStack()
    stack.prom = {"url": "http://prometheus:9090", "status": 200, "value": None}
    assert run(stack)["Prometheus target"].status == FAIL
    stack.prom["value"] = "0"
    assert run(stack)["Prometheus target"].status == FAIL


def test_remediation_silently_disabled_is_a_failure_that_names_the_missing_setting() -> None:
    stack = FakeStack()
    stack.remediation_present -= {"APPROVAL_API_TOKEN"}
    result = run(stack)["remediation"]
    assert result.status == FAIL and "APPROVAL_API_TOKEN" in result.detail
    stack.remediation_present = set(REMEDIATION_NEEDS)
    stack.remediation_extra = "REMEDIATION_ENABLED=0"
    assert run(stack)["remediation"].status == WARN


@pytest.mark.parametrize(("github", "expected"), [
    ({"repo_set": True, "token_set": True, "status": 401}, FAIL),
    ({"repo_set": True, "token_set": True, "status": 404}, FAIL),
    ({"repo_set": True, "token_set": True, "status": 200, "push": False}, WARN),
    ({"repo_set": False, "token_set": True}, FAIL),
])
def test_github_access_is_graded(github, expected) -> None:
    stack = FakeStack()
    stack.github = github
    assert run(stack)["GitHub access"].status == expected


def test_an_unhealthy_or_stopped_container_is_a_failure() -> None:
    stack = FakeStack()
    stack.rows.append({"Service": "mcp-jenkins", "State": "running", "Health": "unhealthy"})
    assert run(stack)["services"].status == FAIL
    stack.rows[-1] = {"Service": "opa", "State": "exited", "Health": ""}
    assert run(stack)["services"].status == FAIL
    stack.rows[-1] = {"Service": "opa", "State": "running", "Health": "starting"}
    assert run(stack)["services"].status == WARN


def test_the_exit_code_is_1_when_anything_fails_and_0_otherwise(monkeypatch, capsys) -> None:
    main = P["main"]

    class Healthy(FakeStack):
        def __init__(self, *a, **k) -> None:
            super().__init__()

    main.__globals__["Stack"] = Healthy
    assert main(["--json"]) == 0
    assert all(item["status"] == PASS for item in json.loads(capsys.readouterr().out))

    class Broken(Healthy):
        def __init__(self, *a, **k) -> None:
            super().__init__()
            self.docker = (1, "permission denied")

    main.__globals__["Stack"] = Broken
    assert main([]) == 1
    assert "[FAIL] jenkins -> docker socket" in capsys.readouterr().out


@pytest.mark.parametrize("source", [LLM_PROBE, MCP_PROBE, PROMETHEUS_PROBE, GITHUB_PROBE])
def test_every_in_container_probe_is_valid_python_and_prints_no_secret(source) -> None:
    compile(source, "<probe>", "exec")
    printed = [line for line in source.splitlines() if "print(" in line]
    assert printed == ["print(json.dumps(out))"]  # one JSON line, built from `out`
    for secret_field in ('out["key"]', 'out["token"]', "out['key']", "out['token']"):
        assert secret_field not in source


def test_the_forbidden_list_matches_what_the_compose_test_forbids() -> None:
    """One list in two places: keep them in step."""
    text = (Path(__file__).resolve().parents[1] / "test_compose.py").read_text(encoding="utf-8")
    for name in JENKINS_FORBIDDEN:
        assert f'"{name}"' in text, f"{name} is forbidden in preflight but not in tests/test_compose.py"

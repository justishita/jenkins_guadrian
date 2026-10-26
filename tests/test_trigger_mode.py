"""TRIGGER_MODE decides who starts an investigation; no incident may be started by two paths.

**Owner:** P2 (Week 5). The Coordinator and the Metrics agent read the same variable: in ``a2a``
mode the Coordinator sends the Metrics agent a task and the Metrics agent serves A2A without
starting its RabbitMQ consumer; in ``rabbitmq`` mode the consumer runs and the Coordinator only
watches for the evidence.
"""

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from coordinator.app import CoordinatorRuntime

REPO = Path(__file__).resolve().parents[1]


def compose_environment(service: str) -> dict[str, str]:
    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))
    environment = compose["services"][service]["environment"]
    if isinstance(environment, list):
        environment = dict(item.split("=", 1) for item in environment)
    return {key: str(value) for key, value in environment.items()}


def test_compose_gives_the_coordinator_and_every_a2a_agent_the_same_trigger_mode():
    modes = {
        service: compose_environment(service)["TRIGGER_MODE"] for service in ("coordinator", "metrics-agent", "code-agent")
    }
    assert set(modes.values()) == {"${TRIGGER_MODE:-a2a}"}, modes


def test_a2a_mode_dispatches_to_all_three_agents_over_a2a(monkeypatch):
    monkeypatch.setenv("TRIGGER_MODE", "a2a")
    monkeypatch.delenv("COORDINATOR_A2A_AGENTS", raising=False)
    assert set(CoordinatorRuntime().a2a_agents) == {"jenkins_agent", "metrics_agent", "code_agent"}


def test_rabbitmq_mode_never_dispatches_to_metrics_or_code(monkeypatch):
    monkeypatch.setenv("TRIGGER_MODE", "rabbitmq")
    monkeypatch.delenv("COORDINATOR_A2A_AGENTS", raising=False)
    runtime = CoordinatorRuntime()
    assert runtime.a2a_agents == ("jenkins_agent",)
    assert not {"metrics_agent", "code_agent"} & set(runtime.a2a_agents)  # they consume the event themselves


def test_an_explicit_agent_list_overrides_the_mode(monkeypatch):
    monkeypatch.setenv("TRIGGER_MODE", "a2a")
    monkeypatch.setenv("COORDINATOR_A2A_AGENTS", "jenkins_agent")
    assert CoordinatorRuntime().a2a_agents == ("jenkins_agent",)


# (package, module holding the RabbitMQ entry point, its function name)
ENTRY_POINTS = [
    ("agents.metrics_agent", "agents.metrics_agent.consumer", "main"),
    ("agents.code_agent", "agents.code_agent.agent", "main"),
]


@pytest.mark.parametrize("mode", ["rabbitmq", "a2a"])
@pytest.mark.parametrize(("package", "rabbit_module", "rabbit_function"), ENTRY_POINTS)
def test_each_agent_entry_point_starts_exactly_one_trigger(mode, package, rabbit_module, rabbit_function):
    """Run ``python -m <agent>`` with both entry points replaced by markers."""
    code = (
        "import sys, types, runpy, importlib\n"
        "started = []\n"
        f"rabbit = types.ModuleType({rabbit_module!r}); rabbit.{rabbit_function} = lambda: started.append('rabbit')\n"
        f"a2a = types.ModuleType({package + '.a2a_app'!r}); a2a.serve = lambda: started.append('a2a')\n"
        f"importlib.import_module({package!r})\n"
        f"sys.modules[{rabbit_module!r}] = rabbit\n"
        f"sys.modules[{package + '.a2a_app'!r}] = a2a\n"
        f"runpy.run_module({package + '.__main__'!r}, run_name='__main__')\n"
        "print(','.join(started))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO,
        env={"TRIGGER_MODE": mode, "PYTHONPATH": str(REPO), "SYSTEMROOT": "C:\\Windows"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ("a2a" if mode == "a2a" else "rabbit")


@pytest.mark.parametrize("package", ["agents.metrics_agent", "agents.code_agent"])
def test_an_unknown_mode_is_refused(package):
    result = subprocess.run(
        [sys.executable, "-m", package],
        cwd=REPO,
        env={"TRIGGER_MODE": "both", "PYTHONPATH": str(REPO), "SYSTEMROOT": "C:\\Windows"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode != 0 and "Unknown TRIGGER_MODE" in result.stderr

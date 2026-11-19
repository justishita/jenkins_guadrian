"""docker-compose.yml: the stack comes back whole, dev tools stay local, and what it needs is documented."""

from pathlib import Path
import re

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
SERVICES = yaml.safe_load(COMPOSE)["services"]

#: Services a person on another machine may need; each has its own sign-in. Everything else is for this machine only.
PUBLIC = {"ui", "agent-api", "jenkins"}
#: No useful in-container probe: a one-line demo app, and a mail catcher image with no shell.
NO_HEALTHCHECK = {"target-app", "mailhog"}


def published(service: dict) -> list[str]:
    return [str(port) for port in service.get("ports", [])]


@pytest.mark.parametrize("name", SERVICES)
def test_every_service_comes_back_by_itself_after_docker_restarts(name) -> None:
    """Only some services did before: after a restart half the stack stayed down for days."""
    assert SERVICES[name].get("restart") == "unless-stopped"


@pytest.mark.parametrize("name", SERVICES)
def test_every_service_is_on_the_one_shared_network(name) -> None:
    assert "devnet" in SERVICES[name]["networks"]


def test_every_dependency_names_a_real_service() -> None:
    for name, service in SERVICES.items():
        for dependency in service.get("depends_on", {}):
            assert dependency in SERVICES, f"{name} depends on {dependency}"


def test_no_host_port_is_published_twice() -> None:
    seen: dict[str, str] = {}
    for name, service in SERVICES.items():
        for port in published(service):
            host = ":".join(port.split(":")[:-1])  # drop the container side
            assert host not in seen, f"{name} and {seen[host]} both publish {host}"
            seen[host] = name


@pytest.mark.parametrize("name", [n for n in SERVICES if n not in PUBLIC])
def test_dev_tools_and_internal_services_listen_on_this_machine_only(name) -> None:
    for port in published(SERVICES[name]):
        assert port.startswith("127.0.0.1:"), f"{name} publishes {port} on every network interface"


def test_the_services_people_reach_from_other_machines_are_the_ones_with_their_own_sign_in() -> None:
    open_services = {name for name, s in SERVICES.items() if any(not p.startswith("127.0.0.1:") for p in published(s))}
    assert open_services == PUBLIC


def test_services_with_a_useful_probe_have_a_healthcheck() -> None:
    assert {name for name, s in SERVICES.items() if "healthcheck" not in s} == NO_HEALTHCHECK


def test_every_variable_the_compose_file_needs_is_in_env_example() -> None:
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    needed = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", COMPOSE))
    missing = sorted(var for var in needed if not re.search(rf"^{var}=", example, re.MULTILINE))
    assert missing == []


def test_the_local_override_is_never_committed_but_its_example_is() -> None:
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "docker-compose.override.yml" in ignored
    assert (ROOT / "docker-compose.override.example.yml").is_file()


def test_the_example_override_only_names_real_services_and_a_real_dockerfile() -> None:
    text = (ROOT / "docker-compose.override.example.yml").read_text(encoding="utf-8")
    # `!override` is a Compose tag plain YAML rejects: drop the tag, keep the structure.
    document = yaml.safe_load(text.replace("!override", ""))
    assert set(document["services"]) <= set(SERVICES)
    assert (ROOT / "ui" / "Dockerfile.prebuilt").is_file()  # the offline-build fallback the example points at
    for service in document["services"].values():
        for port in service.get("ports", []):
            assert re.fullmatch(r"(127\.0\.0\.1:)?\d+:\d+", str(port)), port


@pytest.mark.parametrize("name", ["prometheus", "grafana", "ui"])
def test_new_healthchecks_probe_127_0_0_1_not_localhost(name) -> None:
    """Inside a container `localhost` can resolve to IPv6 (::1) while the server listens on IPv4 only: the nginx UI was
    reported unhealthy while serving fine."""
    command = SERVICES[name]["healthcheck"]["test"][-1]
    assert "127.0.0.1" in command and "localhost" not in command


# ---- Prometheus must scrape services that exist (a stale name made the target "down" and skewed every diagnosis) ----


def scrape_targets() -> list[tuple[str, str, int]]:
    config = yaml.safe_load((ROOT / "monitoring" / "prometheus.yml").read_text(encoding="utf-8"))
    found = []
    for job in config["scrape_configs"]:
        for group in job.get("static_configs", []):
            for target in group["targets"]:
                host, _, port = target.rpartition(":")
                found.append((job["job_name"], host, int(port)))
    return found


def test_every_prometheus_target_is_a_compose_service_or_prometheus_itself() -> None:
    """The target app's compose service was renamed `target-app`, Prometheus kept scraping `target_app`, the name stopped
    resolving, and the Metrics agent then reported "target down" (infra_network_failure, ~0.86) for every incident."""
    for job, host, _ in scrape_targets():
        assert host == "localhost" or host in SERVICES, f"job {job} scrapes {host}, which is not a compose service"


def test_the_target_app_is_scraped_on_the_port_it_listens_on() -> None:
    dockerfile = (ROOT / "target_app" / "Dockerfile").read_text(encoding="utf-8")
    exposed = int(re.search(r"^EXPOSE\s+(\d+)", dockerfile, re.MULTILINE).group(1))
    target = next(t for t in scrape_targets() if t[0] == "target_app")
    assert (target[1], target[2]) == ("target-app", exposed)


def test_the_job_label_the_metrics_agent_queries_still_exists() -> None:
    from agents.metrics_agent.queries import CATALOG

    jobs = {job for job, _, _ in scrape_targets()}
    for spec in CATALOG:
        selected = re.search(r'job="([^"]+)"', spec.promql)
        assert selected is None or selected.group(1) in jobs, f"{spec.name} queries job {selected.group(1)}"


# ---- Jenkins builds run branch code: they must not inherit the stack's secrets (and one of them broke every build) ----


def test_jenkins_does_not_receive_the_whole_env_file() -> None:
    """`env_file: .env` put DATABASE_URL (postgresql+asyncpg://...) into every build; the target app's settings check rejects
    it, so pytest died at collection and no test had ever run. It also handed branch code every secret in .env."""
    assert "env_file" not in SERVICES["jenkins"]


def test_jenkins_gets_every_variable_its_own_config_reads_and_nothing_that_belongs_to_other_services() -> None:
    config = (ROOT / "jenkins" / "jenkins.yaml").read_text(encoding="utf-8") + (ROOT / "jenkins" / "jobs.groovy").read_text(encoding="utf-8")
    needed = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)\}", config)) | set(re.findall(r"getenv\('([A-Z][A-Z0-9_]*)'\)", config))
    provided = set(SERVICES["jenkins"].get("environment", {}))
    # No variable is treated as optional: GITHUB_REPO is "owner/repo" while jobs.groovy wants the owner and the name apart, so
    # its fallbacks build a malformed repository name ("Repository name must be in format owner/repo") when these are missing.
    assert needed <= provided, f"jenkins config reads {sorted(needed - provided)} but the service does not pass it"
    for forbidden in ("DATABASE_URL", "GEMINI_API_KEY", "POSTGRES_PASSWORD", "RABBITMQ_PASSWORD", "APPROVAL_API_TOKEN", "A2A_SHARED_TOKEN", "UI_SESSION_SECRET"):
        assert forbidden not in provided, f"{forbidden} must not reach Jenkins builds"

"""The MCP servers must accept requests addressed to their compose service names.

Found live (2026-11-19): every tool call from the Jenkins agent got HTTP 421 "Invalid Host header: mcp-jenkins:8081", because the
SDK's DNS-rebinding protection only allowed localhost. All tools failed, and the LLM, given no build log, invented a diagnosis
(Maven artifacts and a 600-second timeout for a Python app whose build ran in two seconds).
"""

import importlib
from pathlib import Path
import re

from mcp.server.transport_security import TransportSecurityMiddleware
import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVERS = {"mcp_servers.jenkins_mcp.server": "mcp-jenkins", "mcp_servers.analytics_mcp.server": "mcp-analytics"}


def accepts(settings, host: str) -> bool:
    return TransportSecurityMiddleware(settings)._validate_host(host)


@pytest.mark.parametrize(("module", "service"), SERVERS.items())
def test_the_server_accepts_its_own_service_name_and_loopback(module, service) -> None:
    settings = importlib.import_module(module).mcp.settings.transport_security
    assert settings is not None and settings.enable_dns_rebinding_protection is True  # protection stays on
    for host in (f"{service}:8081", f"{service}:8082", service, "localhost:8081", "127.0.0.1:8082", "[::1]:8081"):
        assert accepts(settings, host), host


@pytest.mark.parametrize(("module", "service"), SERVERS.items())
def test_the_server_still_refuses_other_hosts(module, service) -> None:
    settings = importlib.import_module(module).mcp.settings.transport_security
    for host in ("evil.example.com", "evil.example.com:8081", f"not-{service}:8081", "10.0.0.5:8081", ""):
        assert not accepts(settings, host), host


def test_extra_hosts_can_be_added_from_the_environment(monkeypatch) -> None:
    from mcp_servers.jenkins_mcp.server import _transport_security

    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "my-vm.example.org:*, other-name")
    settings = _transport_security("mcp-jenkins")
    assert accepts(settings, "my-vm.example.org:8081") and accepts(settings, "other-name")


def test_every_default_mcp_url_the_agents_use_names_a_host_the_server_allows() -> None:
    """The agents default to http://mcp-jenkins:8081/mcp and http://mcp-analytics:8082/mcp: those host names must be allowed."""
    client = (ROOT / "common" / "mcp_client.py").read_text(encoding="utf-8")
    urls = re.findall(r'os\.getenv\("MCP_(?:JENKINS|ANALYTICS)_URL", "http://([^/"]+)/mcp"\)', client)
    assert len(urls) == 2
    by_service = {service: importlib.import_module(module).mcp.settings.transport_security for module, service in SERVERS.items()}
    for host_port in urls:
        service = host_port.split(":")[0]
        assert accepts(by_service[service], host_port), f"{host_port} would be refused by the {service} server"

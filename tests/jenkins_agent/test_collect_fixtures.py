from pathlib import Path

import pytest

from agents.jenkins_agent.tools.jenkins_client import ConsoleText
from common.redaction import redact
from scripts import collect_fixtures as collector


def test_redact_masks_compound_assignments_and_bearer_tokens() -> None:
    text = (
        "JENKINS_API_TOKEN=token-value password:pw-value secret=secret-value "
        "apikey=api-value Authorization: Bearer eyJhbGciOi.test-token"
    )

    redacted = redact(text)

    assert "token-value" not in redacted
    assert "pw-value" not in redacted
    assert "secret-value" not in redacted
    assert "api-value" not in redacted
    assert "eyJhbGciOi.test-token" not in redacted
    assert redacted.count("[REDACTED]") == 5


@pytest.mark.asyncio
async def test_collect_fixtures_saves_redacted_console_and_junit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeJenkinsClient:
        async def __aenter__(self) -> "FakeJenkinsClient":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def get_console_text(self, job: str, build_number: int) -> ConsoleText:
            assert (job, build_number) == ("payments/main", 23)
            return ConsoleText("token=console-secret\nBearer bearer-secret", True, False)

        async def get_test_report_xml(self, job: str, build_number: int) -> str:
            assert (job, build_number) == ("payments/main", 23)
            return '<testsuite><failure message="password=xml-secret" /></testsuite>'

    monkeypatch.setattr(collector, "JenkinsClient", FakeJenkinsClient)

    result = await collector.collect_fixtures("payments/main", 23, tmp_path)

    assert result.console_path.name == "payments_main-build-23.log"
    assert result.junit_path == tmp_path / "payments_main-build-23-junit.xml"
    assert result.truncated is True
    assert result.partial is False
    saved_console = result.console_path.read_text(encoding="utf-8")
    saved_xml = result.junit_path.read_text(encoding="utf-8")
    assert "console-secret" not in saved_console
    assert "bearer-secret" not in saved_console
    assert "xml-secret" not in saved_xml
    assert "[REDACTED]" in saved_console
    assert "[REDACTED]" in saved_xml


@pytest.mark.asyncio
async def test_collect_fixtures_skips_absent_junit_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeJenkinsClient:
        async def __aenter__(self) -> "FakeJenkinsClient":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def get_console_text(self, job: str, build_number: int) -> ConsoleText:
            return ConsoleText("clean output", False, False)

        async def get_test_report_xml(self, job: str, build_number: int) -> None:
            return None

    monkeypatch.setattr(collector, "JenkinsClient", FakeJenkinsClient)

    result = await collector.collect_fixtures("no-tests", 1, tmp_path)

    assert result.console_path.read_text(encoding="utf-8") == "clean output"
    assert result.junit_path is None
    assert list(tmp_path.iterdir()) == [result.console_path]
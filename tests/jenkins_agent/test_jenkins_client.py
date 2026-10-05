from collections.abc import AsyncIterator
import logging

import httpx
import pytest
import respx

from agents.jenkins_agent.tools.jenkins_client import (
    JenkinsAuthError,
    JenkinsClient,
    JenkinsNotFound,
    JenkinsUnavailable,
)


BASE_URL = "https://jenkins.test"


def make_client(
    *,
    max_retries: int = 2,
    build_wait_timeout: float = 0.0,
    build_poll_interval: float = 0.01,
) -> JenkinsClient:
    return JenkinsClient(
        base_url=BASE_URL,
        username="jenkins-user",
        api_token="test-token-do-not-log",
        max_retries=max_retries,
        backoff_base=0,
        backoff_max=0,
        build_wait_timeout=build_wait_timeout,
        build_poll_interval=build_poll_interval,
    )


class LargeLogStream(httpx.AsyncByteStream):
    def __init__(self, size: int, prefix_size: int, suffix_size: int) -> None:
        self.size = size
        self.prefix_size = prefix_size
        self.suffix_size = suffix_size

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"A" * self.prefix_size
        middle_size = self.size - self.prefix_size - self.suffix_size
        chunk_size = 65_536
        while middle_size:
            chunk_length = min(middle_size, chunk_size)
            yield b"M" * chunk_length
            middle_size -= chunk_length
        yield b"Z" * self.suffix_size


@pytest.mark.asyncio
async def test_get_build_preserves_running_state_and_encodes_branch_slashes() -> None:
    url = f"{BASE_URL}/job/pipeline/job/feature%252Ftopic/7/api/json"
    with respx.mock(base_url=BASE_URL) as router:
        route = router.get(url).mock(
            return_value=httpx.Response(
                200,
                json={"number": 7, "building": True, "result": None},
            )
        )
        async with make_client() as client:
            build = await client.get_build("pipeline/feature/topic", 7)

    assert route.called
    assert build == {"number": 7, "building": True, "result": None}


@pytest.mark.asyncio
async def test_get_build_refetches_until_build_finishes() -> None:
    url = f"{BASE_URL}/job/pipeline/7/api/json"
    with respx.mock(base_url=BASE_URL) as router:
        route = router.get(url).mock(
            side_effect=[
                httpx.Response(200, json={"number": 7, "building": True}),
                httpx.Response(200, json={"number": 7, "building": False, "result": "FAILURE"}),
            ]
        )
        async with make_client(build_wait_timeout=0.1) as client:
            build = await client.get_build("pipeline", 7)

    assert route.call_count == 2
    assert build == {"number": 7, "building": False, "result": "FAILURE"}


@pytest.mark.asyncio
async def test_get_console_text_streams_and_truncates_50mb() -> None:
    size = 50 * 1024 * 1024
    max_bytes = 100_000
    prefix_size = max_bytes // 5
    suffix_size = max_bytes - prefix_size
    build_url = f"{BASE_URL}/job/pipeline/7/api/json"
    url = f"{BASE_URL}/job/pipeline/7/consoleText"
    with respx.mock(base_url=BASE_URL) as router:
        router.get(build_url).mock(
            return_value=httpx.Response(200, json={"number": 7, "building": False})
        )
        router.get(url).mock(
            return_value=httpx.Response(
                200,
                stream=LargeLogStream(size, prefix_size, suffix_size),
            )
        )
        async with make_client() as client:
            result = await client.get_console_text("pipeline", 7, max_bytes)

    marker = f"[... truncated {size - max_bytes} bytes ...]"
    assert result.truncated is True
    assert result.partial is False
    assert result.text == "A" * prefix_size + marker + "Z" * suffix_size


@pytest.mark.asyncio
async def test_console_text_marks_log_partial_when_build_is_still_running() -> None:
    build_url = f"{BASE_URL}/job/pipeline/7/api/json"
    console_url = f"{BASE_URL}/job/pipeline/7/consoleText"
    with respx.mock(base_url=BASE_URL) as router:
        router.get(build_url).mock(
            return_value=httpx.Response(200, json={"number": 7, "building": True})
        )
        router.get(console_url).mock(return_value=httpx.Response(200, content=b"partial output"))
        async with make_client(build_wait_timeout=0) as client:
            result = await client.get_console_text("pipeline", 7)

    assert result.text == "partial output"
    assert result.truncated is False
    assert result.partial is True


@pytest.mark.asyncio
async def test_console_text_handles_empty_and_non_utf8_logs() -> None:
    empty_build_url = f"{BASE_URL}/job/empty/1/api/json"
    empty_url = f"{BASE_URL}/job/empty/1/consoleText"
    binary_build_url = f"{BASE_URL}/job/binary/1/api/json"
    binary_url = f"{BASE_URL}/job/binary/1/consoleText"
    progress_build_url = f"{BASE_URL}/job/progress/1/api/json"
    progress_url = f"{BASE_URL}/job/progress/1/consoleText"
    with respx.mock(base_url=BASE_URL) as router:
        for build_url in (empty_build_url, binary_build_url, progress_build_url):
            router.get(build_url).mock(
                return_value=httpx.Response(200, json={"building": False})
            )
        router.get(empty_url).mock(return_value=httpx.Response(200, content=b""))
        router.get(binary_url).mock(return_value=httpx.Response(200, content=b"a\xffb\xfe"))
        router.get(progress_url).mock(
            return_value=httpx.Response(
                200,
                content=b"\x1b[31mERROR\x1b[0m\n10%\r20%\r100%\n",
            )
        )
        async with make_client() as client:
            empty_result = await client.get_console_text("empty", 1)
            binary_result = await client.get_console_text("binary", 1)
            progress_result = await client.get_console_text("progress", 1)

    assert (empty_result.text, empty_result.truncated) == ("", False)
    assert (binary_result.text, binary_result.truncated) == ("a\ufffdb\ufffd", False)
    assert progress_result.text == "ERROR\n100%\n"


@pytest.mark.asyncio
async def test_get_stage_summary() -> None:
    url = f"{BASE_URL}/job/pipeline/4/wfapi/describe"
    with respx.mock(base_url=BASE_URL) as router:
        router.get(url).mock(return_value=httpx.Response(200, json={"stages": []}))
        async with make_client() as client:
            summary = await client.get_stage_summary("pipeline", 4)

    assert summary == {"stages": []}


@pytest.mark.asyncio
async def test_get_test_report_returns_none_for_404_and_json_for_existing_report() -> None:
    missing_url = f"{BASE_URL}/job/no-tests/2/testReport/api/json"
    report_url = f"{BASE_URL}/job/with-tests/2/testReport/api/json"
    with respx.mock(base_url=BASE_URL) as router:
        missing = router.get(missing_url).mock(return_value=httpx.Response(404))
        existing = router.get(report_url).mock(
            return_value=httpx.Response(200, json={"totalCount": 1})
        )
        async with make_client() as client:
            no_report = await client.get_test_report("no-tests", 2)
            report = await client.get_test_report("with-tests", 2)

    assert missing.call_count == 1
    assert existing.call_count == 1
    assert no_report is None
    assert report == {"totalCount": 1}


@pytest.mark.asyncio
async def test_get_test_report_xml_returns_xml_or_none() -> None:
    report_url = f"{BASE_URL}/job/with-tests/2/testReport/api/xml"
    missing_url = f"{BASE_URL}/job/no-tests/2/testReport/api/xml"
    with respx.mock(base_url=BASE_URL) as router:
        router.get(report_url).mock(
            return_value=httpx.Response(200, text="<testsuite tests='1'/>")
        )
        router.get(missing_url).mock(return_value=httpx.Response(404))
        async with make_client() as client:
            report = await client.get_test_report_xml("with-tests", 2)
            no_report = await client.get_test_report_xml("no-tests", 2)

    assert report == "<testsuite tests='1'/>"
    assert no_report is None


@pytest.mark.asyncio
async def test_get_build_history_sends_limit_and_returns_builds() -> None:
    url = f"{BASE_URL}/job/pipeline/api/json"
    with respx.mock(base_url=BASE_URL) as router:
        route = router.get(url).mock(
            return_value=httpx.Response(
                200,
                json={"builds": [{"number": 3}, {"number": 2}, {"number": 1}]},
            )
        )
        async with make_client() as client:
            builds = await client.get_build_history("pipeline", limit=2)

    assert route.calls.last.request.url.params["tree"].endswith("{0,2}")
    assert builds == [{"number": 3}, {"number": 2}]


@pytest.mark.asyncio
async def test_get_previous_successful_build() -> None:
    url = f"{BASE_URL}/job/pipeline/lastSuccessfulBuild/api/json"
    with respx.mock(base_url=BASE_URL) as router:
        router.get(url).mock(return_value=httpx.Response(200, json={"number": 12}))
        async with make_client() as client:
            build = await client.get_previous_successful_build("pipeline")

    assert build == {"number": 12}


@pytest.mark.asyncio
async def test_unauthorized_is_not_retried() -> None:
    url = f"{BASE_URL}/job/pipeline/1/api/json"
    with respx.mock(base_url=BASE_URL) as router:
        route = router.get(url).mock(return_value=httpx.Response(401))
        async with make_client() as client:
            with pytest.raises(JenkinsAuthError):
                await client.get_build("pipeline", 1)

    assert route.call_count == 1


@pytest.mark.asyncio
async def test_missing_build_raises_not_found() -> None:
    url = f"{BASE_URL}/job/pipeline/99/api/json"
    with respx.mock(base_url=BASE_URL) as router:
        route = router.get(url).mock(return_value=httpx.Response(404))
        async with make_client() as client:
            with pytest.raises(JenkinsNotFound):
                await client.get_build("pipeline", 99)

    assert route.call_count == 1


@pytest.mark.asyncio
async def test_server_error_retries_then_succeeds() -> None:
    url = f"{BASE_URL}/job/pipeline/1/api/json"
    with respx.mock(base_url=BASE_URL) as router:
        route = router.get(url).mock(
            side_effect=[httpx.Response(500), httpx.Response(200, json={"number": 1})]
        )
        async with make_client(max_retries=1) as client:
            build = await client.get_build("pipeline", 1)

    assert route.call_count == 2
    assert build == {"number": 1}


@pytest.mark.asyncio
async def test_jenkins_restart_503_retries() -> None:
    url = f"{BASE_URL}/job/pipeline/1/api/json"
    with respx.mock(base_url=BASE_URL) as router:
        route = router.get(url).mock(
            side_effect=[httpx.Response(503), httpx.Response(200, json={"number": 1})]
        )
        async with make_client(max_retries=1) as client:
            build = await client.get_build("pipeline", 1)

    assert route.call_count == 2
    assert build == {"number": 1}


@pytest.mark.asyncio
async def test_connection_error_retries() -> None:
    url = f"{BASE_URL}/job/pipeline/1/api/json"
    with respx.mock(base_url=BASE_URL) as router:
        route = router.get(url).mock(
            side_effect=[
                httpx.ConnectError("connection failed"),
                httpx.Response(200, json={"number": 1}),
            ]
        )
        async with make_client(max_retries=1) as client:
            build = await client.get_build("pipeline", 1)

    assert route.call_count == 2
    assert build == {"number": 1}


@pytest.mark.asyncio
async def test_timeout_exhaustion_raises_unavailable() -> None:
    url = f"{BASE_URL}/job/pipeline/1/api/json"
    with respx.mock(base_url=BASE_URL) as router:
        route = router.get(url).mock(
            side_effect=[httpx.ReadTimeout("timed out"), httpx.ReadTimeout("timed out")]
        )
        async with make_client(max_retries=1) as client:
            with pytest.raises(JenkinsUnavailable):
                await client.get_build("pipeline", 1)

    assert route.call_count == 2


@pytest.mark.asyncio
async def test_rotated_console_log_raises_not_found() -> None:
    build_url = f"{BASE_URL}/job/pipeline/7/api/json"
    console_url = f"{BASE_URL}/job/pipeline/7/consoleText"
    with respx.mock(base_url=BASE_URL) as router:
        router.get(build_url).mock(
            return_value=httpx.Response(200, json={"number": 7, "building": False})
        )
        console_route = router.get(console_url).mock(return_value=httpx.Response(404))
        async with make_client() as client:
            with pytest.raises(JenkinsNotFound):
                await client.get_console_text("pipeline", 7)

    assert console_route.call_count == 1


@pytest.mark.asyncio
async def test_auth_failure_logs_clear_message_without_token(caplog: pytest.LogCaptureFixture) -> None:
    url = f"{BASE_URL}/job/pipeline/1/api/json"
    with respx.mock(base_url=BASE_URL) as router:
        router.get(url).mock(return_value=httpx.Response(401))
        async with make_client() as client:
            with caplog.at_level(logging.ERROR, logger="agents.jenkins_agent.tools.jenkins_client"):
                with pytest.raises(JenkinsAuthError):
                    await client.get_build("pipeline", 1)

    assert "Jenkins authentication failed" in caplog.text
    assert "test-token-do-not-log" not in caplog.text


@pytest.mark.asyncio
async def test_environment_configuration_and_default_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JENKINS_URL", BASE_URL)
    monkeypatch.setenv("JENKINS_USER", "env-user")
    monkeypatch.setenv("JENKINS_API_TOKEN", "env-token")
    client = JenkinsClient()
    try:
        assert client.base_url == BASE_URL
        assert client._client.timeout.read == 10.0
        assert client.build_wait_timeout == 30.0
    finally:
        await client.aclose()
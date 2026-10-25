"""Extended unit tests for agents/jenkins_agent/tools/jenkins_client.py."""

import httpx
import pytest
import respx

from agents.jenkins_agent.tools.jenkins_client import (
    JenkinsAuthError,
    JenkinsClient,
    JenkinsError,
    JenkinsNotFound,
    JenkinsUnavailable,
)


def test_jenkins_client_config_validation(monkeypatch):
    """Test validation of credentials and configuration parameters."""
    monkeypatch.delenv("JENKINS_URL", raising=False)
    monkeypatch.delenv("JENKINS_USER", raising=False)
    monkeypatch.delenv("JENKINS_API_TOKEN", raising=False)

    with pytest.raises(ValueError, match="JENKINS_URL must be configured"):
        JenkinsClient(base_url=None)

    with pytest.raises(ValueError, match="JENKINS_USER must be configured"):
        JenkinsClient(base_url="http://jenkins:8080", username=None)

    with pytest.raises(ValueError, match="JENKINS_API_TOKEN must be configured"):
        JenkinsClient(base_url="http://jenkins:8080", username="user", api_token=None)

    with pytest.raises(ValueError, match="max_retries must be non-negative"):
        JenkinsClient(
            base_url="http://jenkins:8080", username="u", api_token="t", max_retries=-1
        )

    with pytest.raises(ValueError, match="backoff delays must be non-negative"):
        JenkinsClient(
            base_url="http://jenkins:8080", username="u", api_token="t", backoff_base=-0.5
        )

    with pytest.raises(ValueError, match="build wait timeout must be non-negative"):
        JenkinsClient(
            base_url="http://jenkins:8080", username="u", api_token="t", build_wait_timeout=-1
        )


def test_job_path_validation():
    """Test job path normalization and validation."""
    with pytest.raises(ValueError, match="job must not be empty"):
        JenkinsClient._job_path("")

    with pytest.raises(ValueError, match="job path components must not be empty"):
        JenkinsClient._job_path([])

    with pytest.raises(ValueError, match="job path components must not be empty"):
        JenkinsClient._job_path([""])

    with pytest.raises(TypeError, match="job path components must be strings"):
        JenkinsClient._job_path([123])  # type: ignore

    # String with slashes
    path = JenkinsClient._job_path("folder/subjob")
    assert path == "/job/folder/job/subjob"

    # Tuple of components
    path2 = JenkinsClient._job_path(("folder", "subjob", "branch"))
    assert path2 == "/job/folder/job/subjob/job/branch"


def test_build_url_validation():
    client = JenkinsClient(
        base_url="http://jenkins:8080",
        username="user",
        api_token="token",
    )
    with pytest.raises(ValueError, match="build number must be non-negative"):
        client._build_url("my-job", -1)

    assert client._build_url("my-job", 5) == "http://jenkins:8080/job/my-job/5"


@pytest.mark.asyncio
async def test_trigger_build_methods():
    """Test trigger_build and trigger_build_url under various HTTP responses."""
    client = JenkinsClient(
        base_url="http://jenkins:8080",
        username="user",
        api_token="token",
        max_retries=1,
        backoff_base=0.01,
    )

    url = "http://jenkins:8080/job/target-app/job/main/build"

    # 1. 201 Created -> success
    with respx.mock:
        respx.post(url).mock(return_value=httpx.Response(201))
        await client.trigger_build("target-app/main", token="build-token")

    # 2. 200 OK -> success
    with respx.mock:
        respx.post(url).mock(return_value=httpx.Response(200))
        await client.trigger_build("target-app/main")

    # 3. 401 Unauthorized -> JenkinsAuthError
    with respx.mock:
        respx.post(url).mock(return_value=httpx.Response(401))
        with pytest.raises(JenkinsAuthError):
            await client.trigger_build("target-app/main")

    # 4. 404 Not Found -> JenkinsNotFound
    with respx.mock:
        respx.post(url).mock(return_value=httpx.Response(404))
        with pytest.raises(JenkinsNotFound):
            await client.trigger_build("target-app/main")

    # 5. 500 Server Error -> JenkinsUnavailable after retries
    with respx.mock:
        respx.post(url).mock(return_value=httpx.Response(500))
        with pytest.raises(JenkinsUnavailable):
            await client.trigger_build("target-app/main")

    # 6. Transport error -> JenkinsUnavailable
    with respx.mock:
        respx.post(url).mock(side_effect=httpx.ConnectError("Connection refused"))
        with pytest.raises(JenkinsUnavailable):
            await client.trigger_build("target-app/main")

    await client.aclose()


@pytest.mark.asyncio
async def test_get_build_history_validation():
    client = JenkinsClient(
        base_url="http://jenkins:8080",
        username="user",
        api_token="token",
    )
    with pytest.raises(ValueError, match="limit must be non-negative"):
        await client.get_build_history("target-app/main", limit=-1)
    await client.aclose()


@pytest.mark.asyncio
async def test_get_previous_successful_build_and_test_report_xml():
    client = JenkinsClient(
        base_url="http://jenkins:8080",
        username="user",
        api_token="token",
    )

    with respx.mock:
        respx.get("http://jenkins:8080/job/target-app/job/main/lastSuccessfulBuild/api/json").mock(
            return_value=httpx.Response(200, json={"number": 40, "result": "SUCCESS"})
        )
        respx.get("http://jenkins:8080/job/target-app/job/main/40/testReport/api/xml").mock(
            return_value=httpx.Response(200, text="<testsuite><testcase name='t1'/></testsuite>")
        )

        prev = await client.get_previous_successful_build("target-app/main")
        assert prev["number"] == 40

        xml = await client.get_test_report_xml("target-app/main", 40)
        assert xml is not None
        assert "<testcase name='t1'/>" in xml

    await client.aclose()


@pytest.mark.asyncio
async def test_console_text_max_bytes_validation():
    client = JenkinsClient(
        base_url="http://jenkins:8080",
        username="user",
        api_token="token",
    )
    with pytest.raises(ValueError, match="max_bytes must be non-negative"):
        await client.get_console_text("target-app/main", 40, max_bytes=-10)
    await client.aclose()

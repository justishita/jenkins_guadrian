"""Tests for the OPA policy gate.

**Owner:** P3. The rules themselves are tested by ``opa test policies``; what matters
here is the client's behaviour at the edges, and above all that it never reports
"allowed" when it did not actually get an allow from OPA.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from coordinator.policy import (
	PULL_REQUEST_PACKAGE,
	REMEDIATION_PACKAGE,
	PolicyGate,
	PolicyResponseInvalid,
	PolicyUnavailable,
	PolicyVerdict,
)


OPA_URL = "http://opa:8181"

PROPOSAL = {
	"incident_id": "11111111-2222-3333-4444-555555555555",
	"confidence": 0.82,
	"changed_files": [{"path": "target_app/config/settings.yaml", "additions": 1, "deletions": 1}],
}


def gate_returning(result: Any, *, status_code: int = 200) -> PolicyGate:
	"""Build a gate backed by a transport that always answers with ``result``."""

	def handler(request: httpx.Request) -> httpx.Response:
		if status_code >= 400:
			return httpx.Response(status_code, text="boom")
		return httpx.Response(status_code, json={"result": result})

	client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
	return PolicyGate(OPA_URL, client=client, max_retries=0)


@pytest.mark.asyncio
async def test_allow_decision_is_reported_as_allowed() -> None:
	async with gate_returning({"allow": True, "deny": [], "warn": []}) as gate:
		verdict = await gate.evaluate_remediation(PROPOSAL)

	assert verdict.allowed is True
	assert bool(verdict) is True
	assert verdict.package == REMEDIATION_PACKAGE
	assert verdict.reason() == "allowed by policy"


@pytest.mark.asyncio
async def test_deny_decision_carries_every_violation_sorted() -> None:
	result = {
		"allow": False,
		"deny": ["proposal deletes the test file x", "confidence 0.3 is below the required 0.5"],
		"warn": ["this is remediation attempt 1"],
	}
	async with gate_returning(result) as gate:
		verdict = await gate.evaluate_remediation(PROPOSAL)

	assert verdict.allowed is False
	assert verdict.violations == (
		"confidence 0.3 is below the required 0.5",
		"proposal deletes the test file x",
	)
	assert verdict.warnings == ("this is remediation attempt 1",)
	assert "below the required" in verdict.reason()


@pytest.mark.asyncio
async def test_warnings_do_not_block() -> None:
	async with gate_returning({"allow": True, "warn": ["check the pinned version"]}) as gate:
		verdict = await gate.evaluate_remediation(PROPOSAL)

	assert verdict.allowed is True
	assert verdict.warnings == ("check the pinned version",)


@pytest.mark.asyncio
async def test_an_undefined_package_is_not_silently_allowed() -> None:
	"""OPA answers `{}` for a package that does not exist - that is not an allow."""
	async with gate_returning(None) as gate:
		with pytest.raises(PolicyResponseInvalid):
			await gate.evaluate_remediation(PROPOSAL)


@pytest.mark.asyncio
async def test_a_result_without_an_allow_rule_is_rejected() -> None:
	async with gate_returning({"deny": []}) as gate:
		with pytest.raises(PolicyResponseInvalid):
			await gate.evaluate_remediation(PROPOSAL)


@pytest.mark.asyncio
async def test_allow_alongside_violations_is_rejected_as_inconsistent() -> None:
	async with gate_returning({"allow": True, "deny": ["touches the Jenkinsfile"]}) as gate:
		with pytest.raises(PolicyResponseInvalid):
			await gate.evaluate_remediation(PROPOSAL)


@pytest.mark.asyncio
async def test_a_client_error_from_opa_is_raised_not_swallowed() -> None:
	async with gate_returning(None, status_code=400) as gate:
		with pytest.raises(PolicyResponseInvalid):
			await gate.evaluate_remediation(PROPOSAL)


@pytest.mark.asyncio
async def test_an_unreachable_opa_blocks_rather_than_allows() -> None:
	def handler(request: httpx.Request) -> httpx.Response:
		raise httpx.ConnectError("connection refused", request=request)

	client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
	async with PolicyGate(OPA_URL, client=client, max_retries=1, backoff_seconds=0) as gate:
		with pytest.raises(PolicyUnavailable):
			await gate.evaluate_remediation(PROPOSAL)


@pytest.mark.asyncio
async def test_a_transient_server_error_is_retried_then_succeeds() -> None:
	attempts: list[int] = []

	def handler(request: httpx.Request) -> httpx.Response:
		attempts.append(1)
		if len(attempts) == 1:
			return httpx.Response(503, text="starting up")
		return httpx.Response(200, json={"result": {"allow": True}})

	client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
	async with PolicyGate(OPA_URL, client=client, max_retries=2, backoff_seconds=0) as gate:
		verdict = await gate.evaluate_remediation(PROPOSAL)

	assert verdict.allowed is True
	assert len(attempts) == 2


@pytest.mark.asyncio
async def test_retries_are_bounded() -> None:
	attempts: list[int] = []

	def handler(request: httpx.Request) -> httpx.Response:
		attempts.append(1)
		return httpx.Response(503, text="still starting")

	client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
	async with PolicyGate(OPA_URL, client=client, max_retries=2, backoff_seconds=0) as gate:
		with pytest.raises(PolicyUnavailable):
			await gate.evaluate_remediation(PROPOSAL)

	assert len(attempts) == 3


@pytest.mark.asyncio
async def test_the_proposal_is_sent_as_the_opa_input_document() -> None:
	seen: dict[str, Any] = {}

	def handler(request: httpx.Request) -> httpx.Response:
		import json

		seen["url"] = str(request.url)
		seen["body"] = json.loads(request.content)
		return httpx.Response(200, json={"result": {"allow": True}})

	client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
	async with PolicyGate(OPA_URL, client=client) as gate:
		await gate.evaluate_pull_request({"policy_allowed": True, "draft": True})

	assert seen["url"] == f"{OPA_URL}/v1/data/{PULL_REQUEST_PACKAGE}"
	assert seen["body"] == {"input": {"policy_allowed": True, "draft": True}}


@pytest.mark.asyncio
async def test_an_unreachable_opa_does_not_leak_a_token_in_the_error() -> None:
	def handler(request: httpx.Request) -> httpx.Response:
		raise httpx.ConnectError("refused by http://opa:8181?token=ghp_abcdef1234567890", request=request)

	client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
	async with PolicyGate(OPA_URL, client=client, max_retries=0) as gate:
		with pytest.raises(PolicyUnavailable) as error:
			await gate.evaluate_remediation(PROPOSAL)

	assert "ghp_abcdef1234567890" not in str(error.value)


def test_a_gate_without_a_url_is_a_configuration_error() -> None:
	with pytest.raises(ValueError):
		PolicyGate("")


def test_a_verdict_without_a_stated_reason_still_explains_itself() -> None:
	verdict = PolicyVerdict(package=REMEDIATION_PACKAGE, allowed=False)
	assert verdict.reason() == "denied by policy without a stated reason"

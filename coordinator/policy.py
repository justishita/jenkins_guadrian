"""Policy gate: ask OPA whether a proposed remediation may proceed.

**Owner:** P3. The rules live in ``policies/*.rego`` and are served by the ``opa``
container; this module is only the client. Keeping the decision in OPA rather than in
Python is the point - the guardrails are reviewable as data, and the same bundle is
checked by ``opa test`` in CI and by ``conftest`` locally.

A gate that fails open is not a gate. When OPA cannot be reached, evaluation raises
``PolicyUnavailable`` and the caller must treat the proposal as blocked.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
from typing import Any

import httpx

from common.redaction import redact


logger = logging.getLogger(__name__)

REMEDIATION_PACKAGE = "jenkinsguardians/remediation"
PULL_REQUEST_PACKAGE = "jenkinsguardians/pullrequest"

_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class PolicyError(RuntimeError):
	"""Base class for policy-evaluation failures."""


class PolicyUnavailable(PolicyError):
	"""OPA could not be reached or did not answer in time."""


class PolicyResponseInvalid(PolicyError):
	"""OPA answered, but not with a decision this code can act on."""


@dataclass(frozen=True)
class PolicyVerdict:
	"""The outcome of evaluating one document against one policy package."""

	package: str
	allowed: bool
	violations: tuple[str, ...] = field(default_factory=tuple)
	warnings: tuple[str, ...] = field(default_factory=tuple)

	def __bool__(self) -> bool:
		return self.allowed

	def reason(self) -> str:
		"""A single line suitable for an audit record or a reviewer notification."""
		if self.allowed:
			return "allowed by policy"
		return "; ".join(self.violations) or "denied by policy without a stated reason"


def _as_messages(value: Any) -> tuple[str, ...]:
	"""Normalise a rule result into a sorted tuple of message strings.

	A partial set rule returns a list; a rule that never fires is absent. Sorting
	keeps audit records stable across evaluations of the same proposal.
	"""
	if value is None:
		return ()
	if isinstance(value, str):
		return (value,)
	if isinstance(value, (list, tuple, set)):
		return tuple(sorted(str(item) for item in value))
	return (str(value),)


class PolicyGate:
	"""Evaluate remediation documents against the OPA policy bundle."""

	def __init__(
		self,
		opa_url: str,
		*,
		timeout_seconds: float = 5.0,
		max_retries: int = 2,
		backoff_seconds: float = 0.5,
		client: httpx.AsyncClient | None = None,
	) -> None:
		if not opa_url:
			raise ValueError("OPA_URL must be configured")
		self._base_url = opa_url.rstrip("/")
		self._timeout = timeout_seconds
		self._max_retries = max_retries
		self._backoff = backoff_seconds
		self._client = client
		self._owns_client = client is None

	async def _http(self) -> httpx.AsyncClient:
		if self._client is None:
			self._client = httpx.AsyncClient(timeout=self._timeout)
		return self._client

	async def close(self) -> None:
		if self._client is not None and self._owns_client:
			await self._client.aclose()
			self._client = None

	async def __aenter__(self) -> "PolicyGate":
		return self

	async def __aexit__(self, *_: object) -> None:
		await self.close()

	async def evaluate(self, package: str, document: dict[str, Any]) -> PolicyVerdict:
		"""Return OPA's verdict for ``document``, or raise if OPA cannot answer.

		``package`` is a slash-separated data path such as
		``jenkinsguardians/remediation``.
		"""
		result = await self._query(package, document)
		if not isinstance(result, dict):
			raise PolicyResponseInvalid(f"policy {package} returned {type(result).__name__}, expected an object")
		if "allow" not in result:
			raise PolicyResponseInvalid(f"policy {package} returned no 'allow' decision")

		violations = _as_messages(result.get("deny"))
		allowed = bool(result["allow"])
		if allowed and violations:
			# allow is derived from deny, so this means the bundle is inconsistent.
			raise PolicyResponseInvalid(
				f"policy {package} allowed a document while reporting {len(violations)} violations"
			)
		return PolicyVerdict(
			package=package,
			allowed=allowed,
			violations=violations,
			warnings=_as_messages(result.get("warn")),
		)

	async def evaluate_remediation(self, proposal: dict[str, Any]) -> PolicyVerdict:
		"""Check a proposed fix before it is shown to a human reviewer."""
		return await self.evaluate(REMEDIATION_PACKAGE, proposal)

	async def evaluate_pull_request(self, request: dict[str, Any]) -> PolicyVerdict:
		"""Check that an approved fix may be opened as a draft pull request."""
		return await self.evaluate(PULL_REQUEST_PACKAGE, request)

	async def _query(self, package: str, document: dict[str, Any]) -> Any:
		url = f"{self._base_url}/v1/data/{package.strip('/')}"
		client = await self._http()
		last_error: Exception | None = None

		for attempt in range(self._max_retries + 1):
			try:
				response = await client.post(url, json={"input": document}, timeout=self._timeout)
			except (httpx.TimeoutException, httpx.TransportError) as error:
				last_error = error
			else:
				if response.status_code not in _RETRYABLE_STATUS:
					if response.status_code >= 400:
						raise PolicyResponseInvalid(
							f"OPA rejected the query for {package}: HTTP {response.status_code}"
						)
					try:
						body = response.json()
					except ValueError as error:
						raise PolicyResponseInvalid(f"OPA returned non-JSON for {package}") from error
					# An undefined package yields `{}` with no "result" key.
					return body.get("result")
				last_error = PolicyUnavailable(f"OPA returned HTTP {response.status_code}")

			if attempt < self._max_retries:
				delay = self._backoff * (2**attempt)
				logger.warning(
					"policy evaluation failed, retrying",
					extra={"package": package, "attempt": attempt + 1, "retry_in_seconds": delay},
				)
				await asyncio.sleep(delay)

		raise PolicyUnavailable(
			f"could not evaluate {package} after {self._max_retries + 1} attempts: {redact(str(last_error))}"
		) from last_error


__all__ = [
	"PULL_REQUEST_PACKAGE",
	"REMEDIATION_PACKAGE",
	"PolicyError",
	"PolicyGate",
	"PolicyResponseInvalid",
	"PolicyUnavailable",
	"PolicyVerdict",
]

"""Shared redaction utilities."""

import re


SECRET_ASSIGNMENT_RE = re.compile(
	r"(?i)([A-Za-z0-9_.-]*(?:token|password|secret|api[_-]?key)\s*[=:]\s*)\S+"
)
BEARER_TOKEN_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*")


def redact(text: str) -> str:
	"""Mask common key/value secrets and HTTP Bearer credentials in text."""
	text = SECRET_ASSIGNMENT_RE.sub(r"\1[REDACTED]", text)
	return BEARER_TOKEN_RE.sub("Bearer [REDACTED]", text)
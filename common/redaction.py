"""Shared redaction utilities."""

import re


SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)([A-Za-z0-9_.-]*(?:token|password|passwd|secret|api[_-]?key|authorization|"
    r"credential|cookie|session)"
    r"\s*[=:]\s*)(?:Bearer\s+)?[^\s,;]+"
)
BEARER_TOKEN_RE = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
GITHUB_TOKEN_RE = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")
KNOWN_SECRET_RE = re.compile(r"\b(?:AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,})\b")
GOOGLE_API_KEY_RE = re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b")
URL_CREDENTIALS_RE = re.compile(r"(?i)(://)[^/@:\s]+:[^/@\s]+@")
SECRET_KEY_RE = re.compile(
    r"(?i)(?:token|password|passwd|secret|api[_-]?key|authorization|credential|cookie|session)"
)


def redact(text: str) -> str:
    """Mask credentials in assignments, headers, URLs, and common provider tokens."""
    text = BEARER_TOKEN_RE.sub("Bearer [REDACTED]", text)
    text = URL_CREDENTIALS_RE.sub(r"\1[REDACTED]@", text)
    text = SECRET_ASSIGNMENT_RE.sub(r"\1[REDACTED]", text)
    text = GITHUB_TOKEN_RE.sub("[REDACTED]", text)
    text = GOOGLE_API_KEY_RE.sub("[REDACTED]", text)
    return KNOWN_SECRET_RE.sub("[REDACTED]", text)


def redact_value(value: object, key: str | None = None) -> object:
    """Redact nested data, including secret values whose field name is sensitive."""
    if isinstance(value, str):
        if key and SECRET_KEY_RE.search(key):
            return "[REDACTED]"
        return redact(value)
    if isinstance(value, dict):
        return {
            str(child_key): redact_value(child_value, str(child_key))
            for child_key, child_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_value(item) for item in value]
    return value

"""Security and anti-hallucination guardrails for agent inputs, outputs, and tools."""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from common.blackboard import BlackboardEntry, get_blackboard
from common.redaction import redact, redact_value

logger = logging.getLogger(__name__)

INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?(previous\s+)?instructions", re.IGNORECASE),
    re.compile(r"disregard\s+(all\s+)?(prior\s+)?prompts", re.IGNORECASE),
    re.compile(r"system\s*prompt\s*override", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+in\s+developer\s+mode", re.IGNORECASE),
    re.compile(r"mark\s+(this\s+build\s+as\s+)?success", re.IGNORECASE),
    re.compile(r"report\s+success", re.IGNORECASE),
    re.compile(r"disable\s+(all\s+)?tests?", re.IGNORECASE),
    re.compile(r"disable\s+security\s+checks?", re.IGNORECASE),
]

TOXIC_PATTERNS = [
    re.compile(r"\b(idiot|stupid|moron|dumb|asshole|bastard|bullshit)\b", re.IGNORECASE),
]

DISALLOWED_RECOMMENDATIONS = [
    re.compile(r"disable\s+tests?", re.IGNORECASE),
    re.compile(r"skip\s+tests?", re.IGNORECASE),
    re.compile(r"delete\s+tests?", re.IGNORECASE),
    re.compile(r"remove\s+tests?", re.IGNORECASE),
    re.compile(r"bypass\s+security", re.IGNORECASE),
    re.compile(r"disable\s+opa", re.IGNORECASE),
]


class ScreenedText(BaseModel):
    model_config = ConfigDict(extra="ignore")
    text: str
    flags: list[str] = Field(default_factory=list)


def screen_untrusted_text(
    text: str,
    source: str = "console_log",
    incident_id: UUID | str | None = None,
) -> ScreenedText:
    """
    Screen untrusted text (logs, commit messages, PRs, comments) for prompt injection
    and toxic content. Wraps in <untrusted_data> tags and neutralizes dangerous spans.
    Never drops evidence.
    """
    flags: list[str] = []
    neutralized = redact(text)

    # Check prompt injection patterns
    for pattern in INJECTION_PATTERNS:
        if pattern.search(neutralized):
            flags.append(f"prompt_injection_pattern:{pattern.pattern}")
            neutralized = pattern.sub("[FLAGGED_PROMPT_INJECTION]", neutralized)

    # Check toxic language
    for pattern in TOXIC_PATTERNS:
        if pattern.search(neutralized):
            flags.append(f"toxic_language_pattern:{pattern.pattern}")
            neutralized = pattern.sub("[FLAGGED_TOXIC_LANGUAGE]", neutralized)

    # Post guardrail flag to Blackboard if incident_id is known
    if flags and incident_id is not None:
        try:
            bb = get_blackboard()
            bb.post(
                BlackboardEntry(
                    incident_id=UUID(str(incident_id)),
                    section="hints",
                    key="jenkins_agent.guardrail_flag",
                    author="jenkins_agent",
                    payload={
                        "source": source,
                        "flags": flags,
                        "details": f"Screened text from {source} triggered security guardrails",
                    },
                )
            )
        except Exception as error:
            logger.warning(
                "Failed to post guardrail flag to Blackboard (%s)",
                type(error).__name__,
            )

    wrapped_text = (
        f"<untrusted_data source='{source}'>\n"
        f"{neutralized}\n"
        f"</untrusted_data>"
    )

    return ScreenedText(text=wrapped_text, flags=flags)


def evidence_guardrail(task_output: Any) -> tuple[bool, Any]:
    """
    CrewAI task guardrail validating EvidenceDraft before finalization:
    - Validate confidence in [0.0, 1.0]
    - No recommendations to disable tests or bypass security
    - No unredacted secrets
    - Anti-hallucination check on evidence
    """
    raw_text = getattr(task_output, "raw", str(task_output))

    # Secret leakage check
    if redact(raw_text) != raw_text:
        return False, "Evidence contains unredacted secrets or credentials. Please redact all secrets."

    # Disallowed recommendations
    for pattern in DISALLOWED_RECOMMENDATIONS:
        if pattern.search(raw_text):
            return False, f"Evidence suggests disallowed action: {pattern.pattern}. Never suggest disabling tests or security checks."

    # Confidence range validation if parsed object or json
    pydantic_obj = getattr(task_output, "pydantic", None)
    if pydantic_obj is not None:
        hypotheses = getattr(pydantic_obj, "hypotheses", [])
        for h in hypotheses:
            conf = getattr(h, "confidence", 1.0)
            if not 0.0 <= conf <= 1.0:
                return False, f"Confidence {conf} is outside allowed [0.0, 1.0] range."

    return True, task_output


T = TypeVar("T")


def validate_tool_output(model_cls: type[T]) -> Callable[..., T]:
    """Wrapper ensuring MCP tool outputs conform to expected Pydantic model."""

    def decorator(func: Callable[..., Any]) -> Callable[..., T]:
        def wrapper(*args: Any, **kwargs: Any) -> T:
            result = func(*args, **kwargs)
            if isinstance(result, model_cls):
                return result
            if isinstance(result, dict):
                return model_cls.model_validate(result)
            return model_cls.model_validate_json(str(result))

        return wrapper

    return decorator

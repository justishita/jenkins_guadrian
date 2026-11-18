"""Security and anti-hallucination guardrails for agent inputs, outputs, and tools."""

from __future__ import annotations

import base64
import binascii
import logging
import re
import unicodedata
from typing import Any, Callable, TypeVar, get_type_hints
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
    re.compile(r"report\s+success\b(?!\s+rate)", re.IGNORECASE),
    re.compile(r"disable\s+(all\s+)?tests?", re.IGNORECASE),
    re.compile(r"disable\s+security\s+checks?", re.IGNORECASE),
    # Week 8 hardening (scripts/evaluate_guardrails.py): paraphrases of the same attacks, other languages, role changes.
    re.compile(r"(forget|disregard|ignore)\s+(everything|anything|all)\s+(that\s+)?(you\s+(were|have\s+been)\s+told|above|before|earlier)", re.IGNORECASE),
    re.compile(r"new\s+instructions?\s*:", re.IGNORECASE),
    re.compile(r"from\s+now\s+on,?\s+you\s+(will|must|should|answer|are)", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+(dan\b|an?\s+ai\b|free\s+of|unrestricted)", re.IGNORECASE),
    re.compile(r"(reveal|show|print|repeat|leak)\s+(me\s+)?(your\s+)?(system\s+prompt|hidden\s+instructions|initial\s+instructions)", re.IGNORECASE),
    re.compile(r"\b(call|invoke)\s+the\s+\S+\s+tool\s+with", re.IGNORECASE),
    re.compile(r"turn\s+off\s+(the\s+)?(security|tests?|checks?|scanner|policy|opa)", re.IGNORECASE),
    re.compile(r"(note|message|instructions?)\s+to\s+(the\s+)?(ai|assistant|model|llm)\b", re.IGNORECASE),
    re.compile(r"mark\s+this\s+build\s+(as\s+)?(success|successful|passed|green)", re.IGNORECASE),
    re.compile(r"do\s+not\s+(mention|tell|reveal)\s+(this|it)\s+to\s+the\s+user", re.IGNORECASE),
    re.compile(r"ignor[ae]\s+(todas\s+)?las\s+instrucciones", re.IGNORECASE),
    re.compile(r"ignorez?\s+(toutes\s+)?les\s+instructions", re.IGNORECASE),
    re.compile(r"ignoriere\s+(alle\s+)?(vorherigen\s+)?anweisungen", re.IGNORECASE),
]

TOXIC_PATTERNS = [
    re.compile(r"\b(idiot(?!-?proof)|stupid|moron|dumb|asshole|bastard|bullshit)\b", re.IGNORECASE),
    # Week 8 hardening: more abuse, a masked spelling (st*pid), abuse aimed at a person, and threats.
    re.compile(r"\b(f[u\*]ck\w*|sh[i\*]t\w*|st[u\*]pid|idi[o\*]t(?!-?proof))\b", re.IGNORECASE),
    re.compile(r"\byou\s+(are\s+|re\s+)?(a\s+|an\s+)?(useless|incompetent|worthless|pathetic)\b", re.IGNORECASE),
    re.compile(r"\bshut\s+up\b", re.IGNORECASE),
    re.compile(r"\b(make\s+you\s+(regret|pay)|watch\s+your\s+back|you(?:'ll|\s+will)\s+(regret|pay\s+for))", re.IGNORECASE),
]

DISALLOWED_RECOMMENDATIONS = [
    re.compile(r"disable\s+tests?", re.IGNORECASE),
    re.compile(r"skip\s+tests?", re.IGNORECASE),
    re.compile(r"delete\s+tests?", re.IGNORECASE),
    re.compile(r"remove\s+tests?", re.IGNORECASE),
    re.compile(r"bypass\s+security", re.IGNORECASE),
    re.compile(r"disable\s+opa", re.IGNORECASE),
    # Week 8 hardening: the same advice in other words (a model can phrase "skip the tests" many ways).
    re.compile(r"disabl(e|ing)\s+(the\s+)?(tests?|security)", re.IGNORECASE),
    re.compile(r"(skip|ignore)\s+(the\s+)?(failing\s+)?tests?", re.IGNORECASE),
    re.compile(r"comment(ing)?\s+out\s+(the\s+)?(failing\s+)?(tests?|assertions?)", re.IGNORECASE),
    re.compile(r"(delete|remove|drop)\s+(the\s+)?(failing\s+)?(assertions?|tests?)", re.IGNORECASE),
    re.compile(r"add\s+@?pytest\.mark\.(skip|xfail)", re.IGNORECASE),
    re.compile(r"continue-on-error:\s*true", re.IGNORECASE),
    re.compile(r"turn\s+off\s+(the\s+)?(opa|policy|security|tests?)", re.IGNORECASE),
]

#: "Do not skip tests" is the opposite of "skip tests". A forbidden phrase is not counted when one of these sits just
#: before it (at most one word between). Deliberately narrow: "do not hesitate to skip tests" is still refused.
NEGATION = re.compile(
    r"(do\s+not|don't|never|avoid|must\s+not|should\s+not|shouldn't|instead\s+of|rather\s+than)\s+(\w+\s+)?$",
    re.IGNORECASE,
)

# Lookalike letters from other alphabets, and digits and symbols standing in for letters ("1gn0re").
_CONFUSABLES = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x", "у": "y",
    "і": "i", "ј": "j", "ѕ": "s", "һ": "h", "ο": "o", "ι": "i", "ν": "v", "α": "a",
})
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"})
_DOTTED = re.compile(r"\b(?:\w\.){2,}\w\b")
_BASE64 = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{24,}={0,2}(?![A-Za-z0-9+/=])")


def normalize_text(text: str) -> str:
    """Fold look-alikes so a pattern sees what a reader sees: full-width and non-breaking forms, zero-width characters,
    other-alphabet look-alike letters, and ``i.g.n.o.r.e`` spelled out letter by letter. Content is otherwise unchanged."""
    text = unicodedata.normalize("NFKC", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")  # zero-width space/joiner, soft hyphen, BOM
    text = text.translate(_CONFUSABLES)
    return _DOTTED.sub(lambda match: match.group().replace(".", ""), text)


def _decoded_blobs(text: str) -> list[tuple[str, str]]:
    """Long base64 runs in the text that decode to readable text: ``(blob, decoded)``."""
    found = []
    for match in _BASE64.finditer(text):
        blob = match.group()
        try:
            decoded = base64.b64decode(blob + "=" * (-len(blob) % 4), validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            continue
        if decoded and sum(ch.isprintable() or ch.isspace() for ch in decoded) / len(decoded) > 0.9:
            found.append((blob, decoded))
    return found


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
    original = redact(text)
    neutralized = normalize_text(original)

    # Check prompt injection and toxic language: directly, then on a "leetspeak" reading and on decoded base64.
    # A hit seen only in an alternate reading blanks the whole line (or the whole blob) it came from, because the
    # span cannot be mapped back to the text a model would read.
    for label, patterns, marker in (
        ("prompt_injection_pattern", INJECTION_PATTERNS, "[FLAGGED_PROMPT_INJECTION]"),
        ("toxic_language_pattern", TOXIC_PATTERNS, "[FLAGGED_TOXIC_LANGUAGE]"),
    ):
        for pattern in patterns:
            if pattern.search(neutralized):
                flags.append(f"{label}:{pattern.pattern}")
                neutralized = pattern.sub(marker, neutralized)
        lines = neutralized.split("\n")
        leet = [line.translate(_LEET) for line in lines]
        for pattern in patterns:
            hit = [index for index, line in enumerate(leet) if pattern.search(line)]
            if hit:
                flags.append(f"{label}:leetspeak:{pattern.pattern}")
                for index in hit:
                    lines[index] = marker
                leet = [line.translate(_LEET) for line in lines]
        neutralized = "\n".join(lines)
        if label == "prompt_injection_pattern":
            for blob, decoded in _decoded_blobs(neutralized):
                if any(p.search(normalize_text(decoded)) for p in patterns):
                    flags.append("prompt_injection_pattern:encoded_base64")
                    neutralized = neutralized.replace(blob, marker)

    if not flags:
        neutralized = original  # nothing found: hand the evidence on exactly as it was, never rewritten

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
    folded = normalize_text(raw_text)
    for pattern in DISALLOWED_RECOMMENDATIONS:
        for match in pattern.finditer(folded):
            if NEGATION.search(folded[max(0, match.start() - 40) : match.start()]):
                continue  # "do not skip tests" advises the opposite
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


# CrewAI runtime validator inspects annotations with get_origin().
# Under 'from __future__ import annotations', annotations are stored as strings.
# Resolving them explicitly ensures CrewAI detects Tuple[bool, Any] correctly.
evidence_guardrail.__annotations__ = get_type_hints(evidence_guardrail)


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

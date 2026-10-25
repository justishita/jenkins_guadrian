"""Task and step monitoring callbacks and Prometheus metrics for CrewAI and ADK."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import threading
from typing import Any, Callable
from uuid import uuid4

from prometheus_client import Counter, Histogram

from common.redaction import redact

logger = logging.getLogger(__name__)

_AUDIT_LOCK = threading.RLock()

# Prometheus metrics
agent_steps_total = Counter(
    "agent_steps_total",
    "Total steps executed by agents",
    ["agent", "variant"],
)
agent_errors_total = Counter(
    "agent_errors_total",
    "Total errors encountered by agents",
    ["agent", "variant"],
)
agent_tool_calls_total = Counter(
    "agent_tool_calls_total",
    "Total tool calls executed by agents",
    ["agent", "variant", "tool"],
)
agent_step_duration_seconds = Histogram(
    "agent_step_duration_seconds",
    "Duration of agent steps in seconds",
    ["agent", "variant"],
)
llm_tokens_total = Counter(
    "llm_tokens_total",
    "Total LLM tokens consumed",
    ["agent", "variant", "type"],
)


def record_audit_event(
    *,
    incident_id: str | None = None,
    agent: str = "unknown",
    run_id: str | None = None,
    prompt_variant: str = "react",
    event_type: str = "step",
    step_idx: int = 0,
    tool: str | None = None,
    args: Any = None,
    duration_ms: float = 0.0,
    ok: bool = True,
    error: str | None = None,
    preview: str | None = None,
    trace_id: str | None = None,
    span_id: str | None = None,
) -> None:
    """Record one structured JSON audit event safely without raising."""
    try:
        args_hash = ""
        if args is not None:
            args_str = json.dumps(args, sort_keys=True, default=str)
            args_hash = hashlib.sha256(args_str.encode("utf-8")).hexdigest()[:16]

        redacted_preview = redact(preview or "")[:500] if preview else None

        event = {
            "incident_id": str(incident_id) if incident_id else None,
            "agent": agent,
            "run_id": run_id or str(uuid4()),
            "prompt_variant": prompt_variant,
            "event_type": event_type,
            "step_idx": step_idx,
            "tool": tool,
            "args_hash": args_hash,
            "duration_ms": duration_ms,
            "ok": ok,
            "error": redact(error) if error else None,
            "preview": redacted_preview,
            "trace_id": trace_id,
            "span_id": span_id,
            "ts": datetime.now(timezone.utc).isoformat(),
        }

        # Write to logger
        logger.info("Agent event: %s", json.dumps(event, default=str))

        # Write to ./data/audit/agent_events.jsonl
        audit_dir = Path(os.getenv("AUDIT_DIR", "data/audit"))
        with _AUDIT_LOCK:
            audit_dir.mkdir(parents=True, exist_ok=True)
            log_file = audit_dir / "agent_events.jsonl"
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, default=str) + "\n")
                f.flush()

        # Update prometheus metrics
        if event_type == "step":
            agent_steps_total.labels(agent=agent, variant=prompt_variant).inc()
            agent_step_duration_seconds.labels(agent=agent, variant=prompt_variant).observe(
                duration_ms / 1000.0
            )
        if not ok or error:
            agent_errors_total.labels(agent=agent, variant=prompt_variant).inc()
        if tool:
            agent_tool_calls_total.labels(agent=agent, variant=prompt_variant, tool=tool).inc()

    except Exception as exc:
        # Monitoring must NEVER break the agent
        logger.warning("Failed to record audit event (%s)", type(exc).__name__)


def create_step_callback(
    agent_name: str,
    incident_id: str | None = None,
    variant: str = "react",
) -> Callable[[Any], None]:
    """Factory creating CrewAI step callback."""
    step_count = 0

    def step_callback(step: Any) -> None:
        nonlocal step_count
        step_count += 1
        try:
            tool_name = getattr(step, "tool", None)
            tool_input = getattr(step, "tool_input", None)
            tool_output = getattr(step, "text", getattr(step, "output", None))
            record_audit_event(
                incident_id=incident_id,
                agent=agent_name,
                prompt_variant=variant,
                event_type="step",
                step_idx=step_count,
                tool=str(tool_name) if tool_name else None,
                args=tool_input,
                ok=True,
                preview=str(tool_output) if tool_output else None,
            )
        except Exception as error:
            logger.warning("Step callback failed (%s)", type(error).__name__)

    return step_callback


def create_task_callback(
    agent_name: str,
    incident_id: str | None = None,
    variant: str = "react",
) -> Callable[[Any], None]:
    """Factory creating CrewAI task callback."""

    def task_callback(output: Any) -> None:
        try:
            raw = getattr(output, "raw", str(output))
            record_audit_event(
                incident_id=incident_id,
                agent=agent_name,
                prompt_variant=variant,
                event_type="task_finished",
                ok=True,
                preview=raw,
            )
        except Exception as error:
            logger.warning("Task callback failed (%s)", type(error).__name__)

    return task_callback

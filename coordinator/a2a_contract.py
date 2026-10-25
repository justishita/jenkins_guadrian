"""Backward-compatible imports for the shared A2A contract."""

from common.a2a_contract import (
    AGENT_KEY,
    AGENT_NAMES,
    EVIDENCE_READY,
    INCIDENT_ID_KEY,
    KIND_KEY,
    A2AContractError,
    agent_card,
    evidence_ready,
    parse_evidence_ready,
)

__all__ = [
    "AGENT_KEY",
    "AGENT_NAMES",
    "EVIDENCE_READY",
    "INCIDENT_ID_KEY",
    "KIND_KEY",
    "A2AContractError",
    "agent_card",
    "evidence_ready",
    "parse_evidence_ready",
]

"""Turns a `scenarios/*.yaml` file with `agent: coordinator` into a fused report.

A coordinator scenario lists what each agent wrote on the blackboard (`agents:`; an agent
that is absent has not reported), optional fusion overrides (`config:`), the author's
ground-truth label (`truth:`, null when the cause is unknown or genuinely ambiguous) and
the whole report structure the Coordinator must produce (`expected:`).

    agents:
      code_agent:
        failure_type: config_error
        confidence: 0.6
        status: completed            # completed | failed | insufficient_evidence
        coverage: 1.0                # investigation_coverage, optional
        contradicts: {dependency_regression: [file-1]}   # contradicting evidence ids per type
"""

from dataclasses import replace
from pathlib import Path
from typing import Any

import yaml

from common.models import FailureTaxonomy
from coordinator.fusion import FusionConfig, fuse
from coordinator.report import FusionMethod, FusedReport
from tests.coordinator.helpers import NOW, make, view

SCENARIO_DIR = Path(__file__).resolve().parents[2] / "scenarios"
AGENT = "coordinator"


def load_coordinator_scenarios() -> list[dict[str, Any]]:
    scenarios: list[dict[str, Any]] = []
    for path in sorted(SCENARIO_DIR.glob("*.yaml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(document, dict), f"{path.name} is not a mapping"
        if document.get("agent") == AGENT:
            document["_file"] = path.name
            scenarios.append(document)
    return scenarios


def scenario_id(scenario: dict[str, Any]) -> str:
    return str(scenario["id"])


def config_for(scenario: dict[str, Any], method: FusionMethod | None = None) -> FusionConfig:
    overrides = dict(scenario.get("config") or {})
    config = replace(FusionConfig(), **overrides)
    return replace(config, method=method) if method is not None else config


def run_scenario(scenario: dict[str, Any], method: FusionMethod | None = None) -> FusedReport:
    evidence = []
    for agent, spec in (scenario.get("agents") or {}).items():
        evidence.append(
            make(
                agent,
                FailureTaxonomy(spec["failure_type"]),
                float(spec["confidence"]),
                status=spec.get("status", "completed"),
                coverage=spec.get("coverage"),
                contradicts={FailureTaxonomy(t): ids for t, ids in (spec.get("contradicts") or {}).items()},
                steps=spec.get("steps"),
            )
        )
    return fuse(view(*evidence), config_for(scenario, method), now=NOW)

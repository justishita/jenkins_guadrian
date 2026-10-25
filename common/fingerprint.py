"""Incident fingerprints shared by the backend and the Jenkins agent.

Lives in ``common/`` because the backend computes it at incident creation and the
Jenkins agent computes it during investigation, and each image ships only its own
package plus ``common/``. Importing it from ``agents.jenkins_agent`` would crash the
backend container at startup.
"""

from __future__ import annotations

import hashlib


def compute_preliminary_fingerprint(job_name: str, failed_stage: str | None) -> str:
    """Compute preliminary fingerprint from job_name + failed_stage.

    Used at incident creation time before log parsing.
    """
    material = f"{job_name}|{failed_stage or ''}".lower().strip()
    return hashlib.sha256(material.encode("utf-8")).hexdigest()

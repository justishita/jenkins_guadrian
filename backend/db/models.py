from datetime import datetime, timezone
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from backend.db.database import Base

# P3 owns the audit schema. Importing it here registers the audit tables on the
# shared metadata so init_db() provisions them alongside `incidents`.
from backend.db import audit_models as audit_models  # noqa: F401


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Incident(Base):
    __tablename__ = "incidents"

    id = Column(String(36), primary_key=True, index=True)
    job_name = Column(String(255), nullable=False)
    build_number = Column(Integer, nullable=False)
    build_url = Column(Text, nullable=False)
    branch = Column(String(255), nullable=False)
    git_commit = Column(String(64), nullable=False)
    failed_stage = Column(String(255), nullable=True)
    status = Column(String(32), default="OPEN", nullable=False)
    event_key = Column(String(64), unique=True, index=True, nullable=False)
    remediation_attempt = Column(Integer, default=0, nullable=False)
    # Set when this incident repeats an earlier one (TC-19), so the duplicate is
    # linked rather than investigated from scratch. Owned by P3 (audit schema).
    related_incident_id = Column(String(36), ForeignKey("incidents.id"), index=True, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "job_name": self.job_name,
            "build_number": self.build_number,
            "build_url": self.build_url,
            "branch": self.branch,
            "git_commit": self.git_commit,
            "failed_stage": self.failed_stage,
            "status": self.status,
            "event_key": self.event_key,
            "remediation_attempt": self.remediation_attempt,
            "related_incident_id": self.related_incident_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

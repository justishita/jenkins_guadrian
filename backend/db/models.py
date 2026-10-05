from datetime import datetime, timezone
from sqlalchemy import Column, DateTime, Integer, String, Text
from backend.db.database import Base


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
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

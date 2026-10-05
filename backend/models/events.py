from datetime import datetime, timezone
from typing import Optional
from pydantic import BaseModel, Field, field_validator


class JenkinsFailureEvent(BaseModel):
    job_name: str = Field(..., description="Name of the Jenkins job")
    build_number: int = Field(..., description="Build number of the failure")
    build_url: str = Field(..., description="Direct URL to the build")
    branch: str = Field(..., description="Git branch name")
    git_commit: str = Field(..., description="Commit SHA that failed")
    failed_stage: Optional[str] = Field(default=None, description="Name of the stage that failed")
    timestamp: datetime = Field(..., description="Time of failure (UTC ISO-8601)")
    incident_id: Optional[str] = Field(default=None, description="Optional incident ID for re-validation tracking")
    remediation_attempt: int = Field(default=0, description="Remediation attempt counter")

    @field_validator("timestamp")
    @classmethod
    def ensure_tz_aware_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            # Treat naive timestamp as UTC
            return v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc)


class WebhookResponse(BaseModel):
    status: str
    incident_id: str
    message: Optional[str] = None


class HealthResponse(BaseModel):
    status: str
    postgres: str
    rabbitmq: str

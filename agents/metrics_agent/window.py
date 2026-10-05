"""Investigation time windows, all timezone-aware UTC."""

from datetime import datetime, timedelta, timezone

from pydantic import BaseModel


class InvestigationWindow(BaseModel):
    """`baseline_start..start` is the normal reference, `start..end` the incident."""

    baseline_start: datetime
    start: datetime
    end: datetime


def build_window(
    failure_time: datetime,
    lookback: timedelta,
    tail: timedelta,
    baseline: timedelta,
    now: datetime | None = None,
) -> InvestigationWindow:
    """Window around `failure_time`; the end is clamped so it never reaches into the future."""
    if failure_time.tzinfo is None:
        raise ValueError("failure_time must be timezone-aware (UTC)")
    failure_time = failure_time.astimezone(timezone.utc)
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    start = failure_time - lookback
    end = min(failure_time + tail, current)
    if end <= start:
        raise ValueError("failure_time is too far in the future for a valid window")
    return InvestigationWindow(baseline_start=start - baseline, start=start, end=end)

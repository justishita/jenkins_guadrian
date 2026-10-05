from datetime import datetime, timedelta, timezone

import pytest

from agents.metrics_agent.window import build_window
from tests.metrics_agent.helpers import FAILURE_TIME, NOW, make_window


def test_window_layout_around_failure() -> None:
    window = make_window()
    assert window.start == FAILURE_TIME - timedelta(minutes=5)
    assert window.baseline_start == window.start - timedelta(minutes=10)
    assert window.end == FAILURE_TIME + timedelta(seconds=60)


def test_window_end_is_clamped_to_now() -> None:
    window = build_window(
        FAILURE_TIME,
        lookback=timedelta(minutes=5),
        tail=timedelta(minutes=10),
        baseline=timedelta(minutes=10),
        now=NOW,
    )
    assert window.end == NOW


def test_window_normalises_offsets_to_utc() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    window = build_window(
        FAILURE_TIME.astimezone(ist), timedelta(minutes=5), timedelta(0), timedelta(minutes=10), now=NOW
    )
    assert window.end == FAILURE_TIME
    assert window.end.utcoffset() == timedelta(0)


def test_naive_failure_time_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        build_window(
            datetime(2026, 10, 5, 9, 30),  # noqa: DTZ001 - naive on purpose
            timedelta(minutes=5),
            timedelta(0),
            timedelta(minutes=10),
        )


def test_failure_in_the_future_rejected() -> None:
    far_future = NOW + timedelta(hours=2)
    with pytest.raises(ValueError, match="future"):
        build_window(far_future, timedelta(minutes=5), timedelta(0), timedelta(minutes=10), now=NOW)

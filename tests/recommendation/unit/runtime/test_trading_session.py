from datetime import datetime, timedelta

import pytest

from trader.recommendation.application.runtime.schedule import SHANGHAI
from trader.recommendation.application.runtime.trading_session import StaleSessionError, TradingSession


@pytest.mark.parametrize(
    ("wall_seconds", "monotonic_seconds", "reason"),
    (
        (-1, 1, "wall_clock_rollback"),
        (60, 1, "clock_divergence"),
        (1, -1, "clock_divergence"),
        (301, 301, "resume_after_suspend"),
        (86400, 86400, "trade_date_changed"),
    ),
)
def test_clock_discontinuity_rejects_old_publication_lease(wall_seconds, monotonic_seconds, reason):
    wall = [datetime(2026, 10, 14, 14, 59, tzinfo=SHANGHAI)]
    elapsed = [100.0]
    session = TradingSession(lambda: wall[0], lambda: elapsed[0])
    old = session.capture()
    wall[0] += timedelta(seconds=wall_seconds)
    elapsed[0] += monotonic_seconds
    with pytest.raises(StaleSessionError):
        with old.guard():
            pytest.fail("old result reached publication")
    current = session.capture()
    assert current.generation == old.generation + 1
    assert session.status().discontinuity_reason == reason
    with current.guard():
        current.remember("new-freeze")
    assert current.allows_version("new-freeze")
    assert not current.allows_version("old-freeze")


def test_continuous_elapsed_time_retains_session_across_freeze_boundary():
    wall = [datetime(2026, 10, 14, 14, 59, 59, tzinfo=SHANGHAI)]
    elapsed = [100.0]
    session = TradingSession(lambda: wall[0], lambda: elapsed[0])
    old = session.capture()
    wall[0] += timedelta(seconds=1)
    elapsed[0] += 1
    with old.guard():
        assert session.capture() == old

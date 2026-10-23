from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from trader.download.domain.qfq_window import CompletedDailyCutoff


@pytest.mark.parametrize(
    "hour,minute,second,expected",
    (
        (15, 0, 0, date(2026, 10, 22)),
        (15, 14, 59, date(2026, 10, 22)),
        (15, 15, 0, date(2026, 10, 23)),
        (15, 15, 1, date(2026, 10, 23)),
    ),
)
def test_completed_daily_cutoff_uses_shanghai_1515(hour, minute, second, expected):
    observed = datetime(2026, 10, 23, hour, minute, second, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert CompletedDailyCutoff(observed) == expected
    assert CompletedDailyCutoff(observed.astimezone(timezone.utc)) == expected


def test_completed_daily_cutoff_rejects_naive_clock():
    with pytest.raises(ValueError, match="aware business clock"):
        CompletedDailyCutoff(datetime(2026, 10, 23, 15, 15))

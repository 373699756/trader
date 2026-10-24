"""V5 reads an extra day without requiring an extra completed input day."""

from dataclasses import replace

import pytest

from tests.component.test_candidate_history_tail import NOW, _Archive
from tests.unit.infra.test_shared_history_projection import _cache


@pytest.mark.parametrize("includes_signal_day", (False, True))
@pytest.mark.parametrize("completed_sessions", (60, 61))
def test_v5_eligibility_uses_sixty_one_completed_days_with_either_history_cutoff(
    completed_sessions, includes_signal_day
):
    source = _Archive(62, 0, ("600001",))
    listed_on = source.current.calendar_dates[-completed_sessions]
    source.current = replace(source.current, listing_intervals=(("600001", listed_on, None),))
    if includes_signal_day:
        source.current = replace(
            source.current,
            data_cutoff=NOW.date(),
            calendar_dates=(*source.current.calendar_dates, NOW.date()),
        )
    cache = _cache(source, 62, sequence=True)
    cache.initialize(cancelled=lambda: False)

    item = cache.completed_model_inputs(("600001",), NOW)[0]

    assert cache.history_sessions == 62
    if completed_sessions == 61:
        assert item.quality.failure is None
        assert item.sequence is not None
        assert len(item.sequence.dates) == 61
        assert item.sequence.dates[-1] < NOW.date()
    else:
        assert item.quality.failure == "history_window_insufficient"
        assert item.sequence is None


def test_v3_completed_history_minimum_does_not_change_for_v5_read_window():
    source = _Archive(61, 0, ("600001",))
    source.current = replace(source.current, listing_intervals=(("600001", source.current.calendar_dates[-60], None),))
    cache = _cache(source, 61)
    cache.initialize(cancelled=lambda: False)

    assert cache.completed_model_inputs(("600001",), NOW)[0].quality.failure == "history_window_insufficient"

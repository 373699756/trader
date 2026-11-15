from __future__ import annotations

import pytest

from trader.download.domain.history_sync import HistorySyncProgress


def test_history_sync_progress_is_a_strict_immutable_value() -> None:
    progress = HistorySyncProgress(
        "downloading_codes",
        "waiting",
        12,
        100,
        current_item="600001",
        attempt=2,
        max_attempts=3,
        call_elapsed_seconds=5.0,
    )

    assert progress.completed_units == 12
    with pytest.raises(ValueError, match="progress"):
        HistorySyncProgress("downloading_codes", "waiting", 101, 100)
    with pytest.raises(ValueError, match="progress"):
        HistorySyncProgress("supplier_calendar", "waiting", 0, 1, attempt=0)
    with pytest.raises(AttributeError):
        progress.completed_units = 13  # type: ignore[misc]


@pytest.mark.parametrize("delay", (0, 15, 30.0, 60.0))
def test_retry_delay_is_a_nonnegative_finite_number_on_retry_events(delay):
    progress = HistorySyncProgress(
        "supplier_daily_raw", "retrying", 0, 1, "300390", attempt=2, max_attempts=4, retry_delay_seconds=delay,
    )
    assert progress.retry_delay_seconds == delay


@pytest.mark.parametrize("delay", (-1, float("inf"), float("nan"), True, "15"))
def test_retry_delay_rejects_invalid_values(delay):
    with pytest.raises(ValueError, match="progress"):
        HistorySyncProgress("supplier_daily_raw", "retrying", 0, 1, retry_delay_seconds=delay)


@pytest.mark.parametrize("state", ("started", "waiting", "completed", "failed", "cancelled"))
def test_retry_delay_cannot_be_attached_to_other_states(state):
    with pytest.raises(ValueError, match="progress"):
        HistorySyncProgress("supplier_daily_raw", state, 0, 1, retry_delay_seconds=15.0)

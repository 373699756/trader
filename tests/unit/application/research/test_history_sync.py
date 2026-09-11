from __future__ import annotations

import pytest

from trader.application.research.history_sync import HistorySyncProgress


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

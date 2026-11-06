"""The history application owns its lease, failure result and initial guard."""

from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from tests.helpers.download.configuration import HistorySyncConfigurationFixture
from trader.download.application.download_history import DownloadHistoryUseCase
from trader.download.application.history_ports import (
    HistorySyncAlreadyRunningError,
    HistorySyncDependencies,
    HistorySyncFailureReport,
)


class _Boundary:
    def __init__(self, failure=None):
        self.events = []
        self.lease_failure = failure
        self.observed_failure = None

    @contextmanager
    def lease(self):
        self.events.append("acquire")
        try:
            if self.lease_failure is not None:
                raise self.lease_failure
            yield
        finally:
            self.events.append("release")

    def load_published_state(self):
        self.events.append("published")
        return None

    def has_space(self, minimum_free_bytes):
        assert minimum_free_bytes == 100
        self.events.append("disk")
        return False

    def finalize(self, result):
        assert result.state == "blocked"
        self.events.append("finalize")

    def safe_snapshot(self):
        self.events.append("safe_snapshot")
        return None

    def failure(self, exc):
        self.observed_failure = exc
        return HistorySyncFailureReport("fixture_io_failed", "bounded_fixture_diagnostic")

    def report_failure(self, failure, progress):
        assert progress is None
        assert failure.reason == "fixture_io_failed"
        self.events.append("report")


def _use_case(tmp_path, boundary):
    configuration = HistorySyncConfigurationFixture(
        history_root=tmp_path / "history",
        reference_root=tmp_path / "reference",
        minimum_free_bytes=100,
    )
    # Unused supplier/task/price methods intentionally do not exist: the
    # disk guard and failed lease must prevent every later operation.
    return DownloadHistoryUseCase(
        configuration,
        object(),
        datetime(2026, 11, 6, 20, tzinfo=ZoneInfo("Asia/Shanghai")),
        lambda: False,
        None,
        HistorySyncDependencies(boundary, boundary, boundary, boundary),
    )


def test_disk_guard_precedes_supplier_and_pending_work_and_releases_lease(tmp_path):
    boundary = _Boundary()
    result = _use_case(tmp_path, boundary).execute()

    assert result.state == "blocked"
    assert result.reason == "disk_space_insufficient"
    assert boundary.events == ["acquire", "published", "disk", "finalize", "release"]


@pytest.mark.parametrize(
    ("failure", "state", "reason"),
    (
        (KeyboardInterrupt(), "cancelled", "cancelled"),
        (HistorySyncAlreadyRunningError(), "already_running", "history_maintenance_running"),
        (OSError("external payload must not become the result"), "failed", "fixture_io_failed"),
    ),
)
def test_failed_lease_produces_a_typed_status_and_releases_resources(tmp_path, failure, state, reason):
    boundary = _Boundary(failure)
    result = _use_case(tmp_path, boundary).execute()

    assert (result.state, result.reason) == (state, reason)
    expected = ["acquire", "release"]
    if state == "failed":
        expected.append("report")
    expected.append("safe_snapshot")
    assert boundary.events == expected
    if state == "failed":
        assert boundary.observed_failure is failure

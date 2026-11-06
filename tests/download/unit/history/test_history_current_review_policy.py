"""The application chooses review sequences and recovery outcomes without SQLite."""

from contextlib import contextmanager
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from tests.helpers.download.configuration import HistorySyncConfigurationFixture
from trader.download.application.download_history import DownloadHistoryUseCase
from trader.download.application.history_ports import (
    HistoryReviewBusyError, HistoryReviewDeferredError, HistoryReviewPreparation,
    HistorySyncDependencies, HistorySyncFailureReport,
)
from trader.download.domain.baostock_daily import BaoStockCalendar, BaoStockSecurity, BaoStockSourceVersions
from trader.download.domain.history_control import HistorySnapshot, HistorySnapshotPartition
from trader.download.domain.history_download import BuildHistoryControlIdentities, HistoryBatchInput, HistoryDownloadBatch
from trader.download.domain.history_reference import HistoryReferenceSnapshot, HistoryStEvidence
from trader.download.domain.history_sync import HistorySupplierContext
from trader.download.domain.price_review import PriceReviewBudgetExhausted

DAY = date(2026, 9, 1)
NOW = datetime(2026, 9, 1, 20, tzinfo=ZoneInfo("Asia/Shanghai"))


def _inputs():
    context = HistorySupplierContext(
        BaoStockCalendar((DAY,)),
        (BaoStockSecurity("600001", "fixture", "main", DAY, None, "fixture"),),
        BaoStockSourceVersions("fixture", "3.10", ()),
        st_evidence=(HistoryStEvidence("600001", DAY, "clear"),),
        reference_id="fixture",
    )
    source, calendar, universe = BuildHistoryControlIdentities(context)
    reference = HistoryReferenceSnapshot(("600001",), (), context.st_evidence)
    snapshot = HistorySnapshot(
        1, DAY, DAY, calendar.content_hash, universe.content_hash, source.content_hash,
        (HistorySnapshotPartition("prices_2026.sqlite3", "a" * 64, 1, 9),),
    )
    batch = HistoryDownloadBatch(2, source.content_hash, calendar.content_hash, universe.content_hash, DAY)
    return HistoryBatchInput(context, source, calendar, universe, reference, batch, False), snapshot


class _Budget:
    def __init__(self, events, failure_check=None):
        self.events = events
        self.failure_check = failure_check
        self.checked = 0

    def check_deadline(self):
        self.checked += 1
        self.events.append("deadline")
        if self.checked == self.failure_check:
            raise PriceReviewBudgetExhausted("deadline")

    def pause(self):
        self.events.append("pause")


class _Review:
    def __init__(self, events, *, changed, failure=None, phase="prepare"):
        self.events = events
        self.changed = changed
        self.failure = failure
        self.phase = phase

    @contextmanager
    def activity(self):
        yield

    def prepare(self, budget):
        self.events.append("prepare")
        if self.phase == "prepare" and self.failure is not None:
            raise self.failure
        return HistoryReviewPreparation((DAY,) if self.changed else ())

    def commit(self, sequence, budget):
        self.events.append(("commit", sequence))
        if self.phase == "commit" and self.failure is not None:
            raise self.failure

    def seal_deferred(self):
        self.events.append("seal")


class _Control:
    def __init__(self, events, inputs):
        self.events = events
        self.inputs = inputs

    def save_inputs(self, parents, reference, publication_id):
        assert parents == (self.inputs.source, self.inputs.calendar, self.inputs.universe)
        assert reference is self.inputs.reference and publication_id == "fixture"
        self.events.append("parents")

    def begin(self, batch, tasks):
        assert batch is self.inputs.batch and tasks == ()
        self.events.append("begin")


class _Prices:
    def __init__(self, review, inputs, snapshot):
        self.review = review
        self.inputs = inputs
        self.snapshot = snapshot

    def current_review(self, inputs, snapshot):
        assert inputs is self.inputs and snapshot is self.snapshot
        return self.review


def _operation(tmp_path, *, changed=True, failure=None, phase="prepare", failure_check=None):
    events = []
    inputs, snapshot = _inputs()
    review = _Review(events, changed=changed, failure=failure, phase=phase)
    control = _Control(events, inputs)
    budget = _Budget(events, failure_check)
    operation = DownloadHistoryUseCase(
        HistorySyncConfigurationFixture(history_root=tmp_path / "history", reference_root=tmp_path / "reference"),
        None, NOW, lambda: False, None,
        HistorySyncDependencies(control, control, control, _Prices(review, inputs, snapshot)),
        price_review_budget=budget,
    )
    return operation, inputs, snapshot, events


@pytest.mark.parametrize("changed", (False, True))
def test_only_new_price_facts_start_an_unpublished_sequence_before_committing(tmp_path, changed):
    operation, inputs, snapshot, events = _operation(tmp_path, changed=changed)

    assert operation._review_current(inputs, snapshot, (), "fixture") is changed

    assert events == (
        ["prepare", "deadline", "parents", "deadline", "begin", ("commit", 2), "pause"]
        if changed else ["prepare", ("commit", 1), "pause"]
    )


@pytest.mark.parametrize("failure_check", (1, 2))
def test_budget_exhaustion_before_begin_does_not_start_a_batch_or_relabel_a_supplier_failure(tmp_path, failure_check):
    operation, inputs, snapshot, events = _operation(tmp_path, failure_check=failure_check)

    assert operation._review_current(inputs, snapshot, (), "fixture") is False

    assert "begin" not in events and "seal" not in events and events[-1] == "pause"
    assert not any(isinstance(event, tuple) for event in events)


@pytest.mark.parametrize("failure", (
    PriceReviewBudgetExhausted("deadline"),
    HistoryReviewBusyError(HistorySyncFailureReport("fixture_busy", "bounded")),
    HistoryReviewBusyError(HistorySyncFailureReport("fixture_busy", "bounded"), partition=True),
))
def test_budget_or_busy_after_begin_preserves_the_pending_batch_and_requests_recovery(tmp_path, failure):
    operation, inputs, snapshot, events = _operation(tmp_path, failure=failure, phase="commit")

    with pytest.raises(HistoryReviewDeferredError, match="history_review_deferred") as caught:
        operation._review_current(inputs, snapshot, (), "fixture")

    assert caught.value.__cause__ is failure
    assert events[-4:] == ["begin", ("commit", 2), "seal", "pause"]


@pytest.mark.parametrize("partition", (False, True))
def test_busy_before_begin_preserves_partition_failure_distinction(tmp_path, partition):
    failure = HistoryReviewBusyError(HistorySyncFailureReport("fixture_busy", "bounded"), partition=partition)
    operation, inputs, snapshot, events = _operation(tmp_path, failure=failure)

    if partition:
        with pytest.raises(HistoryReviewBusyError) as caught:
            operation._review_current(inputs, snapshot, (), "fixture")
        assert caught.value is failure
        assert events == ["prepare", "seal", "pause"]
    else:
        assert operation._review_current(inputs, snapshot, (), "fixture") is False
        assert events == ["prepare", "pause"]


@pytest.mark.parametrize("phase", ("prepare", "commit"))
@pytest.mark.parametrize("failure", (KeyboardInterrupt(), OSError("bounded fixture I/O failure")))
def test_cancellation_and_io_failure_reseal_before_leaving_the_application(tmp_path, phase, failure):
    operation, inputs, snapshot, events = _operation(tmp_path, failure=failure, phase=phase)

    with pytest.raises(type(failure)) as caught:
        operation._review_current(inputs, snapshot, (), "fixture")

    assert caught.value is failure and events[-2:] == ["seal", "pause"]

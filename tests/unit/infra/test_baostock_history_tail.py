from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from tests.component.test_candidate_history_tail import _Archive
from trader.download.domain.history_tail import HistoryTailRequest
from trader.download.infra.baostock_gap_supplier import BaoStockGapRecord, BaoStockGapResult
from trader.download.infra.baostock_history_tail import BaoStockHistoryTailSupplier
from trader.download.infra.history_control_repository import (
    HistoryMaintenanceAlreadyRunningError,
    HistoryMaintenanceLock,
)
from trader.download.infra.history_revision_codec import encode_history_revision


def _request():
    return HistoryTailRequest("600001", tuple(date(2026, 7, 10) + timedelta(days=offset) for offset in range(4)))


def test_same_source_adapter_reads_paired_facts_with_no_retries_and_bounded_deadline(tmp_path):
    calls = []

    def worker(requests, *, options, cancel_requested):
        calls.append(requests)
        assert options.retries == 0
        assert options.call_timeout_seconds == 8.0
        assert options.shutdown_timeout_seconds == 1.0
        assert not cancel_requested()
        records = []
        for request in requests:
            for revision in _Archive.window(request.code, request.trade_dates).revisions:
                key = "unadjusted" if request.family == "daily_raw" else "qfq"
                payload = json.loads(encode_history_revision(revision))["cell"][key]
                records.append(
                    BaoStockGapRecord(
                        request.code,
                        revision.trade_date,
                        request.family,
                        json.dumps(payload, sort_keys=True, separators=(",", ":")),
                    )
                )
        return BaoStockGapResult(tuple(records), ())

    supplier = BaoStockHistoryTailSupplier(tmp_path, worker, cancel_requested=lambda: False, monotonic=lambda: 0.0)
    result = supplier.fetch(_request(), deadline=12.0)
    assert result.source == "baostock"
    assert len(result.cells) == 4
    assert all(cell.status == "complete" for cell in result.cells)
    assert {request.family for request in calls[0]} == {"daily_raw", "daily_qfq"}
    assert tuple(path.name for path in tmp_path.iterdir()) == (".maintenance.lock",)


@pytest.mark.parametrize("case", ("maintenance_busy", "cancelled", "expired"))
def test_tail_adapter_never_waits_on_maintenance_or_starts_an_invalid_request(tmp_path, case):
    def worker(*_args, **_kwargs):
        pytest.fail("blocked requests must not start a supplier worker")

    supplier = BaoStockHistoryTailSupplier(
        tmp_path,
        worker,
        cancel_requested=lambda: case == "cancelled",
        monotonic=lambda: 0.0,
    )
    lock = HistoryMaintenanceLock(tmp_path / ".maintenance.lock")
    try:
        if case == "maintenance_busy":
            lock.acquire()
        expected = (
            HistoryMaintenanceAlreadyRunningError
            if case == "maintenance_busy"
            else RuntimeError
            if case == "cancelled"
            else TimeoutError
        )
        with pytest.raises(expected):
            supplier.fetch(_request(), deadline=0.0 if case == "expired" else 12.0)
    finally:
        lock.release()

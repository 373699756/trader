"""Published history and short-window feedback must reflect actual supplier coverage."""

from dataclasses import replace
from datetime import date, datetime

import pytest

from tests.component.test_qfq_windows import DAYS, SHANGHAI, Supplier, _plain_feedback, _updater, _window
from tests.unit.entrypoints.test_history_sync_progress import _compact_log
from tests.unit.infra.research.test_history_sync import NOW, ProgressRecorder
from tests.unit.infra.test_history_supplier_router import Baseline, Prices, _at, _current_universe, _router
from trader.download.domain.history_supplier_failure import HistorySupplierPendingError
from trader.download.domain.history_sync import HistorySyncConfiguration
from trader.download.entrypoints.history_sync_progress import StderrHistorySyncProgress
from trader.download.infra import history_sync_runner
from trader.download.infra.history_control_index import HistoryControlIndex
from trader.download.infra.history_direct_prices import HistoryDirectPrices
from trader.download.infra.history_download_index import HistoryDownloadIndex
from trader.download.infra.history_snapshot_reader import HistorySnapshotReader
from trader.download.infra.history_sync_runner import RunHistorySync


def test_interrupted_initial_publication_defers_known_invalid_qfq_without_reloading_full_prices(tmp_path, monkeypatch):
    days = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))

    class InvalidQfq(Prices):
        def fetch_window(self, security, dates, **kwargs):
            window = super().fetch_window(security, dates, **kwargs)
            if security.code == "600001":
                partial = replace(
                    window, cells=(replace(window.cells[0], qfq=None, status="qfq_missing"), *window.cells[1:])
                )
                raise HistorySupplierPendingError(partial, "tencent_qfq_invalid_price", (days[0],))
            return window

    supplier = _router(Baseline(days), InvalidQfq(), _current_universe("600001", "600002"))
    config = HistorySyncConfiguration(
        tmp_path, sessions=3, reread_sessions=2, minimum_free_bytes=0, reference_root=tmp_path.parent / "reference"
    )
    publish = history_sync_runner.PublishHistorySnapshot

    def interrupted(*args):
        raise RuntimeError("fixture_publication_failed")

    monkeypatch.setattr(history_sync_runner, "PublishHistorySnapshot", interrupted)
    assert RunHistorySync(config, supplier, clock=lambda: NOW).state == "failed"
    monkeypatch.setattr(history_sync_runner, "PublishHistorySnapshot", publish)

    def no_full_reload(*args):
        raise AssertionError("deferred or completed stock must not reload its download window")

    monkeypatch.setattr(HistoryDirectPrices, "read_for_download", no_full_reload)
    progress = ProgressRecorder()
    result = RunHistorySync(config, supplier, clock=lambda: NOW, progress=progress)
    assert result.state == "completed", result.reason
    assert result.reason == "history_price_gaps_remaining" and result.unresolved_price_cells == 1
    index = HistoryDownloadIndex(tmp_path / "catalog.sqlite3")
    assert index.gaps_by_code() == {"600001": days[:1]}
    assert index.gaps_by_code(days) == {}
    summary = next(item.tencent_summary for item in progress.values if item.tencent_summary is not None)
    assert summary.succeeded_codes == 1 and summary.reused_codes == 1
    assert summary.pending_codes == 1 and summary.failed_codes == 1


def test_rounded_raw_amount_gap_is_completed_without_changing_old_snapshot(tmp_path):
    days = (date(2026, 10, 21), date(2026, 10, 22), date(2026, 10, 23))

    class RefinedPrices(Prices):
        def __init__(self):
            super().__init__()
            self.refined = False

        def fetch_window(self, security, dates, **kwargs):
            window = super().fetch_window(security, dates, **kwargs)
            cells = []
            for cell in window.cells:
                if cell.trade_date != days[-1]:
                    cells.append(cell)
                    continue
                raw = replace(cell.unadjusted, amount=1628277200.0 if self.refined else 1628280000.0)
                qfq = replace(cell.qfq, amount=1628277200.0) if self.refined else None
                cells.append(
                    replace(cell, unadjusted=raw, qfq=qfq, status="complete" if self.refined else "qfq_missing")
                )
            return replace(window, cells=tuple(cells))

    prices = RefinedPrices()
    supplier = _router(Baseline(days), prices, _current_universe("600001"))
    config = HistorySyncConfiguration(
        tmp_path, sessions=3, reread_sessions=2, minimum_free_bytes=0, reference_root=tmp_path.parent / "reference"
    )
    first = RunHistorySync(config, supplier, clock=lambda: _at(days[-1]))
    assert first.unresolved_price_cells == 1
    original = HistoryControlIndex(tmp_path / "catalog.sqlite3").load_state().active_snapshot
    assert original is not None
    old_rows = HistorySnapshotReader(tmp_path).read_day(days[-1], original)

    prices.refined = True
    retried = RunHistorySync(config, supplier, clock=lambda: _at(days[-1]))

    assert retried.state == "completed" and retried.unresolved_price_cells == 0
    assert HistorySnapshotReader(tmp_path).read_day(days[-1], original) == old_rows
    latest = HistoryControlIndex(tmp_path / "catalog.sqlite3").load_state().active_snapshot
    assert latest is not None
    assert all(row.cell.status == "complete" for row in HistorySnapshotReader(tmp_path).read_day(days[-1], latest))


@pytest.mark.parametrize("mode", ("complete", "missing", "pending", "invalid_qfq_price", "unclassified_failure"))
def test_actual_tencent_coverage_controls_retry_and_survives_restart(tmp_path, capsys, mode):
    days = (date(2026, 9, 7), date(2026, 9, 8), date(2026, 9, 9))

    class PartialPrices(Prices):
        def fetch_window(self, security, dates, **kwargs):
            window = super().fetch_window(security, dates, **kwargs)
            if mode == "complete":
                return window
            if mode == "unclassified_failure":
                raise RuntimeError("transport_failed")
            partial = replace(
                window, cells=(replace(window.cells[0], qfq=None, status="qfq_missing"), *window.cells[1:])
            )
            if mode in {"pending", "invalid_qfq_price"}:
                reason = "tencent_qfq_invalid_price" if mode == "invalid_qfq_price" else "tencent_history_pending"
                unusable = (days[0],) if mode == "invalid_qfq_price" else ()
                raise HistorySupplierPendingError(partial, reason, unusable)
            return partial

    baseline = Baseline(days)
    config = HistorySyncConfiguration(
        tmp_path, sessions=3, reread_sessions=2, minimum_free_bytes=0, reference_root=(tmp_path).parent / "reference"
    )
    result = RunHistorySync(
        config, _router(baseline, PartialPrices()), clock=lambda: _at(days[-1]), progress=StderrHistorySyncProgress()
    )
    assert result.state == "completed"
    assert baseline.calls == []
    expected_missing = (
        2 if mode in {"missing", "pending", "invalid_qfq_price"} else 6 if mode == "unclassified_failure" else 0
    )
    assert result.unresolved_price_cells == expected_missing
    logs = _compact_log(capsys.readouterr().err)
    if mode in {"missing", "pending", "invalid_qfq_price"}:
        assert "处理 Tencent下载 | 结果 待重试" in logs
        assert "raw 3/3 | qfq 2/3 | 完整配对 2/3 | 缺日 1 |" in logs
        expected_reason = "tencent_qfq_invalid_price" if mode == "invalid_qfq_price" else "tencent_history_pending"
        assert f" | 原因 {expected_reason}" in logs
        if mode == "invalid_qfq_price":
            assert "其中待重试 2 股" in logs
    else:
        assert "完整 2 股" in logs if mode == "complete" else "失败 2 股" in logs
    previous = HistoryControlIndex(tmp_path / "catalog.sqlite3").load_state().active_snapshot
    old_rows = HistorySnapshotReader(tmp_path).read_day(days[-1], previous)
    baseline.calls.clear()
    retry_prices = Prices()
    retried = RunHistorySync(config, _router(baseline, retry_prices), clock=lambda: _at(days[-1]))
    assert retried.unresolved_price_cells == (2 if mode == "invalid_qfq_price" else 0)
    assert baseline.calls == []
    if mode in {"missing", "pending"}:
        assert len(retry_prices.calls) == 2
    elif mode == "invalid_qfq_price":
        assert retried.state == "already_current"
        assert retried.reason == "history_price_gaps_remaining"
        assert retry_prices.calls == []
    assert HistorySnapshotReader(tmp_path).read_day(days[-1], previous) == old_rows


@pytest.mark.parametrize("repair_retryable", (False, True))
@pytest.mark.parametrize("interrupt_publication", (False, True))
def test_deferred_qfq_gap_is_not_successful_reuse_in_mixed_retry_summary(
    tmp_path, capsys, monkeypatch, repair_retryable, interrupt_publication
):
    days = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))

    class MixedPrices(Prices):
        def __init__(self):
            super().__init__()
            self.repair = False
            self.requested_codes = []

        def fetch_window(self, security, dates, **kwargs):
            self.requested_codes.append(security.code)
            window = super().fetch_window(security, dates, **kwargs)
            if security.code == "600001" or (self.repair and security.code != "600002"):
                return window
            partial = replace(
                window, cells=(replace(window.cells[0], qfq=None, status="qfq_missing"), *window.cells[1:])
            )
            if security.code == "600002":
                raise HistorySupplierPendingError(partial, "tencent_qfq_invalid_price", (days[0],))
            reason = "tencent_history_pending" if security.code == "600003" else "tencent_qfq_page_stalled"
            raise HistorySupplierPendingError(partial, reason)

    prices = MixedPrices()
    baseline = Baseline(days)
    supplier = _router(baseline, prices, _current_universe("600001", "600002", "600003", "600004"))
    config = HistorySyncConfiguration(
        tmp_path, sessions=3, reread_sessions=2, minimum_free_bytes=0, reference_root=(tmp_path).parent / "reference"
    )
    first = RunHistorySync(config, supplier, clock=lambda: NOW)
    assert first.state == "completed" and first.unresolved_price_cells == 3
    control = HistoryControlIndex(tmp_path / "catalog.sqlite3")
    index = HistoryDownloadIndex(tmp_path / "catalog.sqlite3")
    previous = control.load_state().active_snapshot
    assert previous is not None
    reader = HistorySnapshotReader(tmp_path)
    old_rows = reader.read_day(days[0], previous)
    prices.repair = repair_retryable
    prices.requested_codes.clear()

    if interrupt_publication:

        def fail_publication(*_args):
            raise RuntimeError("fixture_publication_failed")

        with monkeypatch.context() as patch:
            patch.setattr(history_sync_runner, "PublishHistorySnapshot", fail_publication)
            interrupted = RunHistorySync(config, supplier, clock=lambda: NOW)
        assert interrupted.state == "failed"
        assert interrupted.reason == "fixture_publication_failed"
        assert control.load_state().active_snapshot == previous
        assert index.pending(previous.sequence) is not None

    progress = StderrHistorySyncProgress()
    result = RunHistorySync(config, supplier, clock=lambda: NOW, progress=progress)
    progress.publish_result(result)

    assert result.state == "completed", result.reason
    assert result.reason == "history_price_gaps_remaining"
    assert result.unresolved_price_cells == (1 if repair_retryable else 3)
    assert set(prices.requested_codes) == {"600003", "600004"}
    assert baseline.calls == []
    assert index.gaps_by_code().get("600002", ()) == (days[0],)
    assert index.gaps_by_code(days).get("600002", ()) == ()
    active = control.load_state().active_snapshot
    assert active is not None and active.sequence == previous.sequence + 1
    assert index.pending(active.sequence) is None
    assert reader.read_day(days[0], previous) == old_rows
    rows = reader.read_day(days[0], active)
    assert next(row.cell.status for row in rows if row.cell.code == "600002") == "qfq_missing"
    expected_status = "complete" if repair_retryable else "qfq_missing"
    assert all(row.cell.status == expected_status for row in rows if row.cell.code in {"600003", "600004"})

    logs = _compact_log(capsys.readouterr().err)
    successful = 3 if repair_retryable else 1
    failed = 0 if repair_retryable else 2
    reused = 3 if repair_retryable and interrupt_publication else 1
    assert f"完整 {successful} 股 | 部分 1 股 | 失败 {failed} 股 | 其中待重试 {failed} 股" in logs
    assert f"其中本地复用 {reused} 股" in logs
    assert "history_validation_failed" not in logs


def test_qfq_transport_failure_preserves_old_window_and_never_calls_baostock(tmp_path):
    class PendingSupplier(Supplier):
        def fetch_window(self, security, dates):
            raise HistorySupplierPendingError(_window(security.code, dates[:-1]))

    messages = []
    updater = replace(
        _updater(tmp_path, PendingSupplier()), report=lambda message: messages.append(_plain_feedback(message))
    )
    updater.seed((_window("600001", price=15),), "old", calendar=DAYS)
    old = updater.v2.read_code("600001")
    observed = datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
    result = updater.execute(observed)
    assert result.pending_codes == 1 and result.completed_codes == 0
    assert updater.v2.read_code("600001") == old
    assert any("处理 Tencent下载 | 结果 待重试" in message for message in messages)
    assert any("原因 tencent_history_pending" in message for message in messages)

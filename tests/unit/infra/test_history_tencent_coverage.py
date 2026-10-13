"""Published history and short-window feedback must reflect actual supplier coverage."""

from dataclasses import replace
from datetime import date, datetime

import pytest

from tests.component.test_qfq_windows import DAYS, SHANGHAI, Supplier, _updater, _window
from tests.unit.infra.research.test_history_archive_sync import NOW
from tests.unit.infra.test_history_supplier_router import Baseline, Prices, _router
from trader.download.domain.history_supplier_failure import HistorySupplierPendingError
from trader.download.domain.history_sync import HistorySyncConfiguration
from trader.download.entrypoints.history_sync_progress import StderrHistorySyncProgress
from trader.download.infra.history_archive_sync import run_history_sync
from trader.download.infra.history_control_index import HistoryControlIndex
from trader.download.infra.history_snapshot_reader import HistorySnapshotReader


@pytest.mark.parametrize("mode", ("complete", "missing", "pending", "unclassified_failure"))
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
            if mode == "pending":
                raise HistorySupplierPendingError(partial)
            return partial

    baseline = Baseline(days)
    config = HistorySyncConfiguration(tmp_path, sessions=3, reread_sessions=2, minimum_free_bytes=0)
    result = run_history_sync(
        config, _router(baseline, PartialPrices()), clock=lambda: NOW, progress=StderrHistorySyncProgress()
    )
    assert result.state == "completed"
    assert baseline.calls == []
    expected_missing = 2 if mode == "pending" else 6 if mode == "unclassified_failure" else 0
    assert result.unresolved_price_cells == expected_missing
    logs = capsys.readouterr().err
    if mode == "pending":
        assert "处理 Tencent下载 | 结果 待重试" in logs
        assert "raw 3/3 | qfq 2/3 | 完整配对 2/3 | 缺日 1 | 原因 tencent_history_pending" in logs
    elif mode == "missing":
        assert "完整 2 股 | 部分 0 股 | 失败 0 股" in logs
    else:
        assert "完整 2 股" in logs if mode == "complete" else "失败 2 股" in logs
    previous = HistoryControlIndex(tmp_path / "control.sqlite3").load_state().active_snapshot
    old_rows = HistorySnapshotReader(tmp_path).read_day(days[-1], previous)
    baseline.calls.clear()
    retried = run_history_sync(config, _router(baseline, Prices()), clock=lambda: NOW)
    assert retried.unresolved_price_cells == 0
    assert baseline.calls == []
    assert HistorySnapshotReader(tmp_path).read_day(days[-1], previous) == old_rows


def test_qfq_transport_failure_preserves_old_window_and_never_calls_baostock(tmp_path):
    class PendingSupplier(Supplier):
        def fetch_window(self, security, dates):
            raise HistorySupplierPendingError(_window(security.code, dates[:-1]))

    messages = []
    updater = replace(_updater(tmp_path, PendingSupplier()), report=messages.append)
    updater.seed((_window("600001", price=15),), "old")
    old = updater.v2.read_code("600001")
    observed = datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
    result = updater.execute(observed)
    assert result.pending_codes == 1 and result.completed_codes == 0
    assert updater.v2.read_code("600001") == old
    assert any("处理 Tencent下载 | 结果 待重试" in message for message in messages)
    assert any("原因 tencent_history_pending" in message for message in messages)

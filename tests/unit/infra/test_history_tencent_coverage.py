"""Published history and short-window feedback must reflect actual supplier coverage."""

from dataclasses import replace
from datetime import date, datetime

import pytest

from tests.component.test_qfq_windows import DAYS, SHANGHAI, Supplier, _plain_feedback, _updater, _window
from tests.unit.entrypoints.test_history_sync_progress import _compact_log
from tests.unit.infra.research.test_history_sync import NOW
from tests.unit.infra.test_history_supplier_router import Baseline, Prices, _router
from trader.download.domain.history_supplier_failure import HistorySupplierPendingError
from trader.download.domain.history_sync import HistorySyncConfiguration
from trader.download.entrypoints.history_sync_progress import StderrHistorySyncProgress
from trader.download.infra.history_control_index import HistoryControlIndex
from trader.download.infra.history_snapshot_reader import HistorySnapshotReader
from trader.download.infra.history_sync_runner import run_history_sync


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
    config = HistorySyncConfiguration(tmp_path, sessions=3, reread_sessions=2, minimum_free_bytes=0)
    result = run_history_sync(
        config, _router(baseline, PartialPrices()), clock=lambda: NOW, progress=StderrHistorySyncProgress()
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
    previous = HistoryControlIndex(tmp_path / "control.sqlite3").load_state().active_snapshot
    old_rows = HistorySnapshotReader(tmp_path).read_day(days[-1], previous)
    baseline.calls.clear()
    retry_prices = Prices()
    retried = run_history_sync(config, _router(baseline, retry_prices), clock=lambda: NOW)
    assert retried.unresolved_price_cells == (2 if mode == "invalid_qfq_price" else 0)
    assert baseline.calls == []
    if mode in {"missing", "pending"}:
        assert len(retry_prices.calls) == 2
    elif mode == "invalid_qfq_price":
        assert retried.state == "already_current"
        assert retried.reason == "history_price_gaps_remaining"
        assert retry_prices.calls == []
    assert HistorySnapshotReader(tmp_path).read_day(days[-1], previous) == old_rows


def test_qfq_transport_failure_preserves_old_window_and_never_calls_baostock(tmp_path):
    class PendingSupplier(Supplier):
        def fetch_window(self, security, dates):
            raise HistorySupplierPendingError(_window(security.code, dates[:-1]))

    messages = []
    updater = replace(
        _updater(tmp_path, PendingSupplier()), report=lambda message: messages.append(_plain_feedback(message))
    )
    updater.seed((_window("600001", price=15),), "old")
    old = updater.v2.read_code("600001")
    observed = datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
    result = updater.execute(observed)
    assert result.pending_codes == 1 and result.completed_codes == 0
    assert updater.v2.read_code("600001") == old
    assert any("处理 Tencent下载 | 结果 待重试" in message for message in messages)
    assert any("原因 tencent_history_pending" in message for message in messages)

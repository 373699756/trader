"""Coverage evidence and durable asynchronous writes share one completion boundary."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, timedelta
from threading import Event, Lock

import pytest

from tests.unit.infra.research.test_history_archive_sync import NOW, FakeSupplier, ProgressRecorder, _configuration
from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.domain.history_price_coverage import HistoryPriceCoverage
from trader.download.domain.history_revision import HistoryTrainingPoint, HistoryTrainingRow
from trader.download.domain.history_trading_status import HistoryTradingFact
from trader.download.entrypoints.history_sync_progress import StderrHistorySyncProgress
from trader.download.infra.history_archive_sync import run_history_sync
from trader.download.infra.history_control_index import HistoryControlIndex
from trader.download.infra.history_direct_prices import HistoryDirectPrices
from trader.download.infra.history_download_index import HistoryDownloadIndex
from trader.download.infra.history_month_partition import HistoryMonthPrices
from trader.download.infra.history_snapshot_reader import HistorySnapshotReader
from trader.training.infra.profile.v2.contracts import V2_TRAINING_PROFILE
from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE
from trader.training.infra.sample_builder import _SampleBuildState

DAYS = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))


class SuspendedSupplier(FakeSupplier):
    def __init__(self, *, evidence=True):
        super().__init__(DAYS)
        self.evidence = evidence
        self.status_calls = []

    def fetch_tencent_window(self, security, dates):
        window = super().fetch_tencent_window(security, dates)
        return replace(
            window,
            cells=tuple(
                replace(cell, status="unknown_missing", unadjusted=None, qfq=None)
                if cell.trade_date == DAYS[1]
                else cell
                for cell in window.cells
            ),
        )

    def fetch_trading_facts(self, security, dates):
        self.status_calls.append((security.code, dates))
        return (
            tuple(HistoryTradingFact(security.code, day, "suspended", False) for day in dates) if self.evidence else ()
        )

    def fetch_baostock_prices(self, security, dates):
        raise AssertionError("suspension or unverified absence must not request prices")


@pytest.mark.parametrize("evidence", [True, False])
def test_status_only_evidence_closes_suspension_without_fabricating_prices(tmp_path, capsys, evidence):
    supplier = SuspendedSupplier(evidence=evidence)
    progress = StderrHistorySyncProgress()
    result = run_history_sync(_configuration(tmp_path), supplier, clock=lambda: NOW, progress=progress)
    progress.publish_result(result)
    assert result.state == "completed", result.reason
    assert result.unresolved_price_cells == (0 if evidence else 2)
    assert all(dates == (DAYS[1],) for _, dates in supplier.status_calls)
    active = HistoryControlIndex(tmp_path / "control.sqlite3").load_state().active_snapshot
    reader = HistorySnapshotReader(tmp_path)
    cells = reader.read_day(DAYS[1], active)
    assert all(row.cell.status == ("supplier_marked_suspended" if evidence else "unknown_missing") for row in cells)
    assert all(row.cell.qfq is None or row.cell.qfq.close_price is None for row in cells)
    training = tuple(reader.iter_training_rows(active, DAYS, ("600001", "600002")))
    assert all(row.point is None for row in training if row.trade_date == DAYS[1])
    assert {row.trade_date for row in training} == set(DAYS)
    logs = capsys.readouterr().err
    if evidence:
        assert "覆盖完成（已持久化）" in logs and "已确认停牌 1 日" in logs
        supplier.status_calls.clear()
        supplier.calls.clear()
        assert run_history_sync(_configuration(tmp_path), supplier, clock=lambda: NOW).unresolved_price_cells == 0
        assert not supplier.status_calls and not supplier.calls
    else:
        assert "数据不完整" in logs


def test_long_suspension_counts_as_coverage_but_not_price_rows():
    coverage = HistoryPriceCoverage(2000, 1435, 1435, 1435, 565, 2000)
    assert coverage.missing == 0
    with pytest.raises(ValueError):
        HistoryPriceCoverage(2000, 1435, 1435, 1435, 566)


@pytest.mark.parametrize("profile", [V2_TRAINING_PROFILE, V3_TRAINING_PROFILE])
@pytest.mark.parametrize("explicit_suspension", [True, False])
def test_training_calendar_gap_resets_features_and_never_shifts_tomorrow(profile, explicit_suspension):
    class Sink:
        def add_final(self, samples):
            assert not samples

    days = tuple(date(2025, 1, 1) + timedelta(days=i) for i in range(profile.history_sessions + 2))
    state = _SampleBuildState(
        Sink(),
        profile,
        None,
        len(days),
        {day: i for i, day in enumerate(days)},
        frozenset(days),
        frozenset(("600001",)),
    )
    for day in days[:-2]:
        state.accept(HistoryTrainingRow("600001", day, "main", "industry", HistoryTrainingPoint(day, 10.0, 1000.0)))
    candidate = state.pending_days[-1].samples["600001"]
    if explicit_suspension:
        state.accept(HistoryTrainingRow("600001", days[-2], "main", "industry", None))
    state.accept(
        HistoryTrainingRow("600001", days[-1], "main", "industry", HistoryTrainingPoint(days[-1], 11.0, 1000.0))
    )
    assert len(state.rolling_codes["600001"].points) == 1
    assert candidate.future_returns == [None] * 5
    assert not state.pending_days[-1].samples


@pytest.mark.parametrize("delisted", [False, True])
def test_listing_boundaries_are_reported_without_rows_outside_listing(tmp_path, capsys, delisted):
    class NewListing(FakeSupplier):
        def load_context(self, as_of, sessions):
            security = BaoStockSecurity(
                "600001", "fixture", "main", DAYS[0] if delisted else DAYS[1], DAYS[-1] if delisted else None, "test"
            )
            return super().load_context(as_of, sessions, universe=(security,))

    result = run_history_sync(
        _configuration(tmp_path), NewListing(DAYS), clock=lambda: NOW, progress=StderrHistorySyncProgress()
    )
    assert result.state == "completed" and result.unresolved_price_cells == 0
    logs = capsys.readouterr().err
    assert "本次应有 2 日" in logs and "上市前/退市后不适用 1 日" in logs


def test_slow_writer_keeps_network_busy_and_checkpoint_waits_for_commit(tmp_path, monkeypatch):
    started = Event()
    reached_capacity = Event()
    guard = Lock()
    network_count = 0
    count_at_write = []

    class ManySecurities(FakeSupplier):
        def load_context(self, as_of, sessions):
            universe = tuple(
                BaoStockSecurity(f"{600000 + i}", "fixture", "main", date(2000, 1, 1), None, "test") for i in range(80)
            )
            return super().load_context(as_of, sessions, universe=universe)

        def fetch_tencent_window(self, security, dates):
            nonlocal network_count
            window = super().fetch_tencent_window(security, dates)
            with guard:
                network_count += 1
                if network_count == 64:
                    reached_capacity.set()
            return window

    original = HistoryDirectPrices.commit

    def slow_commit(prices, results):
        if not started.is_set():
            count_at_write.append(network_count)
            started.set()
            assert reached_capacity.wait(5), "network must continue while writer is blocked"
            assert network_count == 64, "completed plus in-flight payloads must stay bounded"
            tasks = HistoryDownloadIndex(tmp_path / "parallel/control.sqlite3").tasks()
            assert not any(task.tencent_done for task in tasks)
        return original(prices, results)

    monkeypatch.setattr(HistoryDirectPrices, "commit", slow_commit)
    config = replace(_configuration(tmp_path / "parallel"), history_workers=2)
    progress = ProgressRecorder()
    with ThreadPoolExecutor(max_workers=2) as network, ThreadPoolExecutor(max_workers=1) as writer:
        result = run_history_sync(
            config, ManySecurities(DAYS), clock=lambda: NOW, worker_pool=network, writer_pool=writer, progress=progress
        )
    assert result.state == "completed", result.reason
    assert count_at_write[0] < 64
    assert all(task.tencent_done for task in HistoryDownloadIndex(config.archive_root / "control.sqlite3").tasks())
    assert any(item.pipeline and item.pipeline.write_seconds > 0 for item in progress.values)
    monkeypatch.setattr(HistoryDirectPrices, "commit", original)
    serial = tmp_path / "serial"
    assert run_history_sync(_configuration(serial), ManySecurities(DAYS), clock=lambda: NOW).state == "completed"
    parallel_snapshot = HistoryControlIndex(config.archive_root / "control.sqlite3").load_state().active_snapshot
    serial_snapshot = HistoryControlIndex(serial / "control.sqlite3").load_state().active_snapshot
    assert parallel_snapshot.content_hash == serial_snapshot.content_hash


@pytest.mark.parametrize("failure", [OSError("disk_full"), KeyboardInterrupt()])
def test_async_write_failure_never_acknowledges_or_enters_baostock(tmp_path, monkeypatch, failure):
    supplier = FakeSupplier(DAYS)
    original = HistoryDirectPrices.commit

    def fail(prices, results):
        raise failure

    monkeypatch.setattr(HistoryDirectPrices, "commit", fail)
    with ThreadPoolExecutor(max_workers=2) as network, ThreadPoolExecutor(max_workers=1) as writer:
        result = run_history_sync(
            _configuration(tmp_path), supplier, clock=lambda: NOW, worker_pool=network, writer_pool=writer
        )
    assert result.state == ("failed" if isinstance(failure, OSError) else "cancelled")
    assert not any(task.tencent_done for task in HistoryDownloadIndex(tmp_path / "control.sqlite3").tasks())
    assert "baostock_prices" not in supplier.events
    monkeypatch.setattr(HistoryDirectPrices, "commit", original)
    assert run_history_sync(_configuration(tmp_path), supplier, clock=lambda: NOW).state == "completed"


def test_async_cross_month_failure_reuses_durable_rows_on_restart(tmp_path, monkeypatch):
    days = (date(2026, 8, 31), date(2026, 9, 1), date(2026, 9, 2))
    supplier = FakeSupplier(days)
    original = HistoryMonthPrices.save_revision_batches

    def fail_second_month(month, revisions):
        if revisions[0].trade_date.month == 9:
            raise OSError("disk_full")
        return original(month, revisions)

    monkeypatch.setattr(HistoryMonthPrices, "save_revision_batches", fail_second_month)
    with ThreadPoolExecutor(max_workers=2) as network, ThreadPoolExecutor(max_workers=1) as writer:
        result = run_history_sync(
            _configuration(tmp_path), supplier, clock=lambda: NOW, worker_pool=network, writer_pool=writer
        )
    assert result.state == "failed"
    assert not any(task.tencent_done for task in HistoryDownloadIndex(tmp_path / "control.sqlite3").tasks())
    monkeypatch.setattr(HistoryMonthPrices, "save_revision_batches", original)
    monkeypatch.setattr(supplier, "fetch_tencent_window", lambda *_args: (_ for _ in ()).throw(RuntimeError("offline")))
    result = run_history_sync(_configuration(tmp_path), supplier, clock=lambda: NOW)
    assert result.state == "completed" and result.unresolved_price_cells == 4
    active = HistoryControlIndex(tmp_path / "control.sqlite3").load_state().active_snapshot
    assert all(row.cell.status == "complete" for row in HistorySnapshotReader(tmp_path).read_day(days[0], active))

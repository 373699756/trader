"""Coverage evidence and durable asynchronous writes share one completion boundary."""

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, timedelta
from itertools import count
from threading import Event, Lock, get_ident

import pytest

from tests.unit.entrypoints.test_history_sync_progress import _compact_log
from tests.unit.infra.research.test_history_sync import NOW, FakeSupplier, ProgressRecorder, _configuration, _download
from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.domain.history_price_coverage import HistoryPriceCoverage
from trader.download.domain.history_revision import HistoryRevision, HistoryTrainingPoint, HistoryTrainingRow
from trader.download.entrypoints.history_sync_progress import StderrHistorySyncProgress
from trader.download.infra import history_download_flow as module
from trader.download.infra.history_control_index import HistoryControlIndex
from trader.download.infra.history_direct_prices import HistoryDirectPrices
from trader.download.infra.history_download_index import HistoryDownloadIndex
from trader.download.infra.history_prices import HistoryPricePartition, HistoryPriceWriteSession
from trader.download.infra.history_snapshot_reader import HistorySnapshotReader
from trader.download.infra.history_sqlite_activity import HistorySqliteActivity
from trader.download.infra.history_sync_runner import RunHistorySync
from trader.training.infra.profile.v2.contracts import V2_TRAINING_PROFILE
from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE
from trader.training.infra.sample_builder import _SampleBuildState

DAYS = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))


def test_recovery_reads_each_physical_year_once_and_keeps_sequence_visibility(tmp_path, monkeypatch):
    days = (date(2025, 1, 2), date(2025, 2, 3), date(2025, 12, 4), date(2026, 1, 5))
    cells = _download("600001", days).batch.cells
    for year in (2025, 2026):
        partition = HistoryPricePartition(tmp_path / f"prices_{year}.sqlite3", year, 1)
        partition.initialize()
        partition.save_revisions(
            tuple(
                HistoryRevision(2 if cell.trade_date == days[-1] else 1, "main", cell, None, None, None)
                for cell in cells
                if cell.trade_date.year == year
            )
        )
    opened = []
    read_connection = HistoryPricePartition._read_connection

    def counted_read(partition):
        opened.append(partition._path.name)
        return read_connection(partition)

    monkeypatch.setattr(HistoryPricePartition, "_read_connection", counted_read)
    prices = HistoryDirectPrices(tmp_path, 1, None, HistorySqliteActivity(lambda: None, lambda: False))
    assert prices.read_for_download("600001", days) == cells[:-1]
    assert opened == ["prices_2025.sqlite3", "prices_2026.sqlite3"]


def test_waiting_pipeline_reports_active_jobs_without_advancing_durable_completion(tmp_path, monkeypatch):
    ticks = count()
    release = Event()
    progress = []

    class TestClock:
        @staticmethod
        def monotonic():
            return float(next(ticks))

    class WaitingSupplier(FakeSupplier):
        def fetch_tencent_window(self, security, dates):
            assert release.wait(10)
            return super().fetch_tencent_window(security, dates)

    class Recorder:
        def publish(self, value):
            progress.append(value)
            if value.state == "waiting" and value.pipeline and value.pipeline.inflight_codes:
                release.set()

    monkeypatch.setattr(module, "time", TestClock)
    with ThreadPoolExecutor(max_workers=1) as network, ThreadPoolExecutor(max_workers=1) as writer:
        result = RunHistorySync(
            replace(_configuration(tmp_path), history_workers=1),
            WaitingSupplier(DAYS),
            clock=lambda: NOW,
            worker_pool=network,
            writer_pool=writer,
            progress=Recorder(),
        )
    assert result.state == "completed", result.reason
    value = next(item for item in progress if item.state == "waiting" and item.pipeline)
    assert value.completed_units == 0 and value.pipeline.downloaded_codes == 0
    assert value.pipeline.inflight_codes == 1 and value.pipeline.pending_codes == 1
    assert value.pipeline.oldest_inflight_seconds > 0


class ShortTencentSupplier(FakeSupplier):
    def __init__(self):
        super().__init__(DAYS)

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


def test_inferred_suspension_is_published_without_a_price_or_retry_gap(tmp_path, capsys):
    class InferredTencentSupplier(FakeSupplier):
        def fetch_tencent_window(self, security, dates):
            window = super().fetch_tencent_window(security, dates)
            return replace(
                window,
                cells=tuple(
                    replace(cell, status="tencent_inferred_suspended", unadjusted=None, qfq=None)
                    if cell.trade_date == DAYS[1]
                    else cell
                    for cell in window.cells
                ),
            )

    supplier = InferredTencentSupplier(DAYS)
    progress = StderrHistorySyncProgress()
    result = RunHistorySync(_configuration(tmp_path), supplier, clock=lambda: NOW, progress=progress)
    progress.publish_result(result)
    assert result.state == "completed" and result.unresolved_price_cells == 0
    active = HistoryControlIndex(tmp_path / "catalog.sqlite3").load_state().active_snapshot
    reader = HistorySnapshotReader(tmp_path)
    assert all(row.cell.status == "tencent_inferred_suspended" for row in reader.read_day(DAYS[1], active))
    training = tuple(reader.iter_training_rows(active, DAYS, ("600001", "600002")))
    assert all(
        row.point is None and row.cell_status == "tencent_inferred_suspended"
        for row in training
        if row.trade_date == DAYS[1]
    )
    assert all(row.point is not None for row in training if row.trade_date != DAYS[1])
    assert "推定停牌 1" in _compact_log(capsys.readouterr().err)
    supplier.calls.clear()
    assert RunHistorySync(_configuration(tmp_path), supplier, clock=lambda: NOW).unresolved_price_cells == 0
    assert not supplier.calls


def test_explicit_unknown_tencent_cell_remains_retryable_without_baostock(tmp_path, capsys):
    supplier = ShortTencentSupplier()
    progress = StderrHistorySyncProgress()
    result = RunHistorySync(_configuration(tmp_path), supplier, clock=lambda: NOW, progress=progress)
    progress.publish_result(result)
    assert result.state == "completed", result.reason
    assert result.unresolved_price_cells == 2
    assert "baostock_prices" not in supplier.events
    active = HistoryControlIndex(tmp_path / "catalog.sqlite3").load_state().active_snapshot
    reader = HistorySnapshotReader(tmp_path)
    cells = reader.read_day(DAYS[1], active)
    assert all(row.cell.status == "unknown_missing" for row in cells)
    assert all(row.cell.qfq is None or row.cell.qfq.close_price is None for row in cells)
    training = tuple(reader.iter_training_rows(active, DAYS, ("600001", "600002")))
    assert all(row.point is None for row in training if row.trade_date == DAYS[1])
    assert {row.trade_date for row in training} == set(DAYS)
    logs = _compact_log(capsys.readouterr().err)
    assert "Tencent下载 | 完成" in logs
    assert "股票 600001 600001" in logs
    assert "raw 2/3 | qfq 2/3 | 完整配对 2/3 | 缺日 1 |" in logs
    assert " | 原因 tencent_history_pending" in logs
    tencent_lines = [line for line in logs.splitlines() if "Tencent下载" in line]
    last_stock = max(index for index, line in enumerate(tencent_lines) if "股票" in line)
    assert all("等待" not in line for line in tencent_lines[last_stock + 1 :])
    repaired_supplier = FakeSupplier(DAYS)
    assert RunHistorySync(_configuration(tmp_path), repaired_supplier, clock=lambda: NOW).unresolved_price_cells == 0
    assert len(repaired_supplier.calls) == 2
    assert all(row.cell.status == "unknown_missing" for row in reader.read_day(DAYS[1], active))


def test_short_tencent_response_reports_actual_price_rows():
    coverage = HistoryPriceCoverage(2000, 1435, 1435, 1435, calendar_sessions=2000)
    assert coverage.missing == 565
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


@pytest.mark.parametrize("profile", [V2_TRAINING_PROFILE, V3_TRAINING_PROFILE])
def test_inferred_suspension_preserves_trade_bar_features_but_not_tomorrow_label(profile):
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
    state.accept(HistoryTrainingRow("600001", days[-2], "main", "industry", None, "tencent_inferred_suspended"))
    state.accept(
        HistoryTrainingRow("600001", days[-1], "main", "industry", HistoryTrainingPoint(days[-1], 11.0, 1000.0))
    )
    assert len(state.rolling_codes["600001"].points) == profile.history_sessions
    assert state.pending_days[-1].samples["600001"].trade_date == days[-1]
    assert candidate.future_returns[0] is None
    assert candidate.future_returns[1] == pytest.approx(0.1)


@pytest.mark.parametrize("delisted", [False, True])
def test_listing_boundaries_are_reported_without_rows_outside_listing(tmp_path, capsys, delisted):
    class NewListing(FakeSupplier):
        def load_context(self, as_of, sessions):
            security = BaoStockSecurity(
                "600001", "fixture", "main", DAYS[0] if delisted else DAYS[1], DAYS[-1] if delisted else None, "test"
            )
            return super().load_context(as_of, sessions, universe=(security,))

    result = RunHistorySync(
        _configuration(tmp_path), NewListing(DAYS), clock=lambda: NOW, progress=StderrHistorySyncProgress()
    )
    assert result.state == "completed" and result.unresolved_price_cells == 0
    logs = _compact_log(capsys.readouterr().err)
    assert "raw 2/2 | qfq 2/2 | 完整配对 2/2 | 缺日 0 |" in logs
    assert " | 原因 无" in logs
    assert "/3" not in logs


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

    original = HistoryDirectPrices.commit_pipeline

    def slow_commit(prices, results):
        if not started.is_set():
            count_at_write.append(network_count)
            started.set()
            assert reached_capacity.wait(5), "network must continue while writer is blocked"
            assert network_count == 64, "completed plus in-flight payloads must stay bounded"
            tasks = HistoryDownloadIndex(tmp_path / "parallel/catalog.sqlite3").tasks()
            assert not any(task.tencent_done for task in tasks)
        return original(prices, results)

    monkeypatch.setattr(HistoryDirectPrices, "commit_pipeline", slow_commit)
    config = replace(_configuration(tmp_path / "parallel"), history_workers=2)
    progress = ProgressRecorder()
    with ThreadPoolExecutor(max_workers=2) as network, ThreadPoolExecutor(max_workers=1) as writer:
        result = RunHistorySync(
            config, ManySecurities(DAYS), clock=lambda: NOW, worker_pool=network, writer_pool=writer, progress=progress
        )
    assert result.state == "completed", result.reason
    assert count_at_write[0] < 64
    assert all(task.tencent_done for task in HistoryDownloadIndex(config.archive_root / "catalog.sqlite3").tasks())
    assert any(item.pipeline and item.pipeline.write_seconds > 0 for item in progress.values)
    monkeypatch.setattr(HistoryDirectPrices, "commit_pipeline", original)
    serial = tmp_path / "serial"
    assert RunHistorySync(_configuration(serial), ManySecurities(DAYS), clock=lambda: NOW).state == "completed"
    parallel_snapshot = HistoryControlIndex(config.archive_root / "catalog.sqlite3").load_state().active_snapshot
    serial_snapshot = HistoryControlIndex(serial / "catalog.sqlite3").load_state().active_snapshot
    assert parallel_snapshot.content_hash == serial_snapshot.content_hash


def test_pipeline_reuses_month_connection_and_checkpoints_each_write_batch_once(tmp_path, monkeypatch):
    class FortySecurities(FakeSupplier):
        def load_context(self, as_of, sessions):
            universe = tuple(
                BaoStockSecurity(f"{600000 + i}", "fixture", "main", date(2000, 1, 1), None, "test") for i in range(40)
            )
            return super().load_context(as_of, sessions, universe=universe)

    opened_months = []
    checkpoint_sizes = []
    write_sizes = []
    prepare_threads = set()
    writer_threads = set()
    open_session = HistoryPricePartition.open_write_session
    finish = HistoryDownloadIndex.finish
    commit = HistoryDirectPrices.commit_pipeline
    prepare = HistoryDirectPrices.prepare

    def tracked_prepare(prices, *args, **kwargs):
        prepare_threads.add(get_ident())
        return prepare(prices, *args, **kwargs)

    def tracked_open(month, *, fresh=False, pending_sequence=None):
        opened_months.append(1)
        return open_session(month, fresh=fresh, pending_sequence=pending_sequence)

    def tracked_finish(index, completions):
        checkpoint_sizes.append(len(completions))
        return finish(index, completions)

    def tracked_commit(prices, results):
        writer_threads.add(get_ident())
        outcome = commit(prices, results)
        write_sizes.append(len(results))
        return outcome

    monkeypatch.setattr(HistoryDirectPrices, "prepare", tracked_prepare)
    monkeypatch.setattr(HistoryPricePartition, "open_write_session", tracked_open)
    monkeypatch.setattr(HistoryDownloadIndex, "finish", tracked_finish)
    monkeypatch.setattr(HistoryDirectPrices, "commit_pipeline", tracked_commit)
    with ThreadPoolExecutor(max_workers=2) as network, ThreadPoolExecutor(max_workers=1) as writer:
        result = RunHistorySync(
            _configuration(tmp_path),
            FortySecurities(DAYS),
            clock=lambda: NOW,
            worker_pool=network,
            writer_pool=writer,
        )

    assert result.state == "completed", result.reason
    assert opened_months == [1]
    assert sum(checkpoint_sizes) == 40
    assert len(checkpoint_sizes) < 40
    assert prepare_threads and writer_threads and prepare_threads.isdisjoint(writer_threads)
    # Network completion may leave a singleton tail; each write batch still has one checkpoint.
    assert checkpoint_sizes == write_sizes
    assert all(1 <= size <= 64 for size in checkpoint_sizes)
    assert any(size > 1 for size in checkpoint_sizes)


def test_transient_worker_and_writer_rejection_preserves_complete_history(tmp_path):
    class RejectOnce:
        def __init__(self, delegate):
            self.delegate = delegate
            self.rejections = 0

        def submit(self, function, /, *args, **kwargs):
            if not self.rejections:
                self.rejections += 1
                return None
            return self.delegate.submit(function, *args, **kwargs)

    supplier = FakeSupplier(DAYS)
    with ThreadPoolExecutor(max_workers=2) as network, ThreadPoolExecutor(max_workers=1) as writer:
        download_pool, write_pool = RejectOnce(network), RejectOnce(writer)
        result = RunHistorySync(
            _configuration(tmp_path),
            supplier,
            clock=lambda: NOW,
            worker_pool=download_pool,
            writer_pool=write_pool,
        )
    assert result.state == "completed", result.reason
    assert download_pool.rejections == write_pool.rejections == 1
    assert len(supplier.calls) == 2
    assert all(task.tencent_done for task in HistoryDownloadIndex(tmp_path / "catalog.sqlite3").tasks())
    active = HistoryControlIndex(tmp_path / "catalog.sqlite3").load_state().active_snapshot
    assert active is not None
    assert len(HistorySnapshotReader(tmp_path).read_day(DAYS[-1], active)) == 2
    supplier.calls.clear()
    assert RunHistorySync(_configuration(tmp_path), supplier, clock=lambda: NOW).state == "already_current"
    assert not supplier.calls


def test_permanent_worker_rejection_reports_capacity_failure_and_resumes(tmp_path):
    class RejectAll:
        def __init__(self):
            self.codes: set[str] = set()

        def submit(self, _function, /, *args, **_kwargs):
            self.codes.add(args[0].code)
            return None

    pool = RejectAll()
    supplier = FakeSupplier(DAYS)
    started = time.monotonic()
    failed = RunHistorySync(_configuration(tmp_path), supplier, clock=lambda: NOW, worker_pool=pool)
    elapsed = time.monotonic() - started
    assert failed.state == "failed" and failed.reason == "history_worker_unavailable"
    assert len(pool.codes) == 1 and 4.5 <= elapsed < 9
    assert not supplier.calls
    assert not any(task.tencent_done for task in HistoryDownloadIndex(tmp_path / "catalog.sqlite3").tasks())
    assert HistoryControlIndex(tmp_path / "catalog.sqlite3").load_state().active_snapshot is None

    resumed = RunHistorySync(_configuration(tmp_path), supplier, clock=lambda: NOW)
    assert resumed.state == "completed", resumed.reason
    assert len(supplier.calls) == 2
    assert all(task.tencent_done for task in HistoryDownloadIndex(tmp_path / "catalog.sqlite3").tasks())


@pytest.mark.parametrize("failure", [OSError("disk_full"), KeyboardInterrupt()])
def test_async_write_failure_never_acknowledges_or_enters_baostock(tmp_path, monkeypatch, failure):
    supplier = FakeSupplier(DAYS)
    original = HistoryDirectPrices.commit_pipeline

    def fail(prices, results):
        raise failure

    monkeypatch.setattr(HistoryDirectPrices, "commit_pipeline", fail)
    with ThreadPoolExecutor(max_workers=2) as network, ThreadPoolExecutor(max_workers=1) as writer:
        result = RunHistorySync(
            _configuration(tmp_path), supplier, clock=lambda: NOW, worker_pool=network, writer_pool=writer
        )
    assert result.state == ("failed" if isinstance(failure, OSError) else "cancelled")
    assert not any(task.tencent_done for task in HistoryDownloadIndex(tmp_path / "catalog.sqlite3").tasks())
    assert "baostock_prices" not in supplier.events
    monkeypatch.setattr(HistoryDirectPrices, "commit_pipeline", original)
    assert RunHistorySync(_configuration(tmp_path), supplier, clock=lambda: NOW).state == "completed"


def test_async_cross_year_failure_reuses_durable_rows_on_restart(tmp_path, monkeypatch):
    days = (date(2025, 12, 31), date(2026, 1, 1), date(2026, 1, 2))
    supplier = FakeSupplier(days)
    original = HistoryPriceWriteSession.save_revision_batches

    def fail_second_year(partition, revisions):
        if revisions[0].trade_date.year == 2026:
            raise OSError("disk_full")
        return original(partition, revisions)

    monkeypatch.setattr(HistoryPriceWriteSession, "save_revision_batches", fail_second_year)
    with ThreadPoolExecutor(max_workers=2) as network, ThreadPoolExecutor(max_workers=1) as writer:
        result = RunHistorySync(
            _configuration(tmp_path), supplier, clock=lambda: NOW, worker_pool=network, writer_pool=writer
        )
    assert result.state == "failed"
    assert not any(task.tencent_done for task in HistoryDownloadIndex(tmp_path / "catalog.sqlite3").tasks())
    monkeypatch.setattr(HistoryPriceWriteSession, "save_revision_batches", original)
    monkeypatch.setattr(supplier, "fetch_tencent_window", lambda *_args: (_ for _ in ()).throw(RuntimeError("offline")))
    result = RunHistorySync(_configuration(tmp_path), supplier, clock=lambda: NOW)
    assert result.state == "completed" and result.unresolved_price_cells == 4
    active = HistoryControlIndex(tmp_path / "catalog.sqlite3").load_state().active_snapshot
    assert all(row.cell.status == "complete" for row in HistorySnapshotReader(tmp_path).read_day(days[0], active))

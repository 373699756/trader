from __future__ import annotations

from tests.helpers.shared.configuration import SqlitePolicyFixture

import json
import sqlite3
import time
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from tests.helpers.download.qfq import QFQ_CONFIGURATION, QFQ_TENCENT_OPTIONS, publish_windows
from trader.download.application.read_published_history import ReadPublishedHistoryUseCase
from trader.download.application.update_qfq import QfqPairPublicationError, QfqWriterDrainError, UpdateQfqWindows
from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.domain.published_history import PublishedHistoryWindow
from trader.download.domain.qfq_window import (
    QfqPendingReason,
    QfqUpdateResult,
    QfqWindowIncompleteError,
)
from trader.infra.shutdown import ShutdownBudget
from trader.download.infra.qfq_sqlite import SQLiteQfqWindowCache
from trader.download.domain.qfq_planning import QfqNonTradingFact
from trader.download.infra.tencent_qfq_supplier import TencentQfqDependencies, TencentQfqSupplier
from trader.infra.sqlite_codec import EncodeDate
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction
from trader.infra.settings.models import HistoryPreloadSettings
from trader.recommendation.infra.market_data.published_history_cache import PublishedHistoryCache
from trader.recommendation.infra.market_data.shared_history_projection import SharedHistoryProjection
from tests.helpers.download.history.qfq_windows import (
    DAYS,
    SHANGHAI,
    Supplier,
    _IncompleteSupplier,
    _fingerprints,
    _plain_feedback,
    _reference,
    _updater,
    _window,
)


def test_shared_pair_and_checkpoint_commit_once_and_noop_preserves_bytes(tmp_path):
    primary = SQLiteQfqWindowCache(tmp_path / "daily_window.sqlite3", "v2", sqlite_policy=SqlitePolicyFixture())
    peer = SQLiteQfqWindowCache(primary.path, "v3", sqlite_policy=SqlitePolicyFixture())
    windows = (_window(days=DAYS[-251:]), _window("300001", days=DAYS[-251:]))
    changed, rows = primary.replace_paired_windows(
        peer, windows, "fixture", QFQ_CONFIGURATION, calendar=DAYS[-251:], completed_day=DAYS[-1]
    )
    assert changed == ("daily_window.sqlite3",) and rows == 502
    assert len(primary.read_code("600001").cells) == 251
    assert len(peer.read_code("600001").cells) == 61
    assert primary.completed(DAYS[-1], "600001", "fixture")
    assert not primary.completed(DAYS[-1], "600001", "other")
    assert len(tuple(tmp_path.glob("*.sqlite3"))) == 1
    before = _fingerprints(tmp_path)
    assert primary.replace_paired_windows(
        peer, windows, "fixture", QFQ_CONFIGURATION, calendar=DAYS[-251:], completed_day=DAYS[-1]
    ) == (
        (),
        0,
    )
    assert _fingerprints(tmp_path) == before


@pytest.mark.parametrize("broken_report", (False, True))
def test_qfq_cancelled_writer_keeps_deferred_publication_pending(tmp_path, monkeypatch, broken_report):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    entered, release = Event(), Event()
    entered_at = []
    sealing, finish_sealing = Event(), Event()
    original = SQLiteQfqWindowCache.replace_paired_windows
    publish = SQLiteQfqWindowCache.publish_file

    def blocked_write(cache, *args, **kwargs):
        result = original(cache, *args, **kwargs)
        entered_at.append(time.monotonic())
        entered.set()
        assert release.wait(5)
        return result

    def blocked_publish(cache):
        if entered.is_set():
            sealing.set()
            assert finish_sealing.wait(5)
        return publish(cache)

    monkeypatch.setattr(SQLiteQfqWindowCache, "replace_paired_windows", blocked_write)
    monkeypatch.setattr(SQLiteQfqWindowCache, "publish_file", blocked_publish)
    network, writer = ThreadPoolExecutor(max_workers=1), ThreadPoolExecutor(max_workers=1)

    def report(_message):
        if broken_report and entered.is_set():
            raise OSError("controlled report failure")

    updater = replace(
        _updater(tmp_path, Supplier()), worker_pool=network, writer_pool=writer,
        cancel_requested=entered.is_set, shutdown=ShutdownBudget(0.05), report=report,
    )
    observed = datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
    try:
        with pytest.raises(QfqWriterDrainError) as failure:
            updater.execute(observed)
        assert entered.is_set()
        assert time.monotonic() - entered_at[0] < 2
        assert not failure.value.pending.done()
        release.set()
        assert sealing.wait(5)
        assert not failure.value.pending.done()
        finish_sealing.set()
        failure.value.pending.result(timeout=5)
    finally:
        release.set()
        finish_sealing.set()
        writer.shutdown(wait=True)
        network.shutdown(wait=True)
    updater.v2.verify_integrity()
    assert updater.v2.read_code("600001").cells


def test_batch_iteration_opens_once_per_32_codes_and_closes_before_yield(tmp_path, monkeypatch):
    from trader.download.infra import qfq_sqlite as module

    primary = SQLiteQfqWindowCache(tmp_path / "daily_window.sqlite3", "v2", sqlite_policy=SqlitePolicyFixture())
    windows = tuple(_window(f"600{number:03d}", days=DAYS[-5:]) for number in range(65))
    publish_windows(primary, windows, "fixture")
    manifest = primary.manifest()
    assert manifest is not None
    opened = []
    original = module.OpenSqliteConnection

    def connect(*args, **kwargs):
        connection = original(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(module, "OpenSqliteConnection", connect)
    iterator = primary.iter_windows(manifest, sessions=5)
    assert next(iterator) == windows[0]
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].run("SELECT 1")
    iterator.close()
    opened.clear()
    assert tuple(primary.iter_windows(manifest, sessions=5)) == windows
    assert len(opened) == 3
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.run("SELECT 1")


def test_pair_failure_rolls_back_prices_both_identities_and_checkpoint(tmp_path, monkeypatch):
    from trader.infra.sqlite_connection import SqliteConnection

    primary = SQLiteQfqWindowCache(tmp_path / "daily_window.sqlite3", "v2", sqlite_policy=SqlitePolicyFixture())
    peer = SQLiteQfqWindowCache(primary.path, "v3", sqlite_policy=SqlitePolicyFixture())
    publish_windows(primary, (_window(),), "old")
    original = SqliteConnection.run

    def fail(self, sql, parameters=()):
        if sql.startswith("INSERT OR REPLACE INTO window_binding") and parameters[0] == "v3":
            raise sqlite3.OperationalError("fixture interrupted")
        return original(self, sql, parameters)

    monkeypatch.setattr(SqliteConnection, "run", fail)
    with pytest.raises(QfqPairPublicationError) as error:
        primary.replace_paired_windows(
            peer,
            (_window(days=DAYS[-251:], price=12.0),),
            "new",
            QFQ_CONFIGURATION,
            calendar=DAYS[-251:],
            completed_day=DAYS[-1],
        )
    assert error.value.codes == () and error.value.rows == 0
    assert primary.source_identity("600001") == peer.source_identity("600001") == "old"
    assert primary.read_code("600001").cells[-1].qfq.close_price == 10.0
    assert not primary.completed(DAYS[-1], "600001", "new")


def test_sparse_windows_use_market_calendar_not_last_traded_rows(tmp_path):
    primary = SQLiteQfqWindowCache(tmp_path / "daily_window.sqlite3", "v2", sqlite_policy=SqlitePolicyFixture())
    peer = SQLiteQfqWindowCache(primary.path, "v3", sqlite_policy=SqlitePolicyFixture())
    window = _window(days=tuple(day for index, day in enumerate(DAYS[-251:]) if index % 3 == 0))
    primary.replace_paired_windows(
        peer, (window,), "fixture", QFQ_CONFIGURATION, calendar=DAYS[-251:], completed_day=DAYS[-1]
    )
    assert peer.read_code("600001").cells == tuple(cell for cell in window.cells if cell.trade_date >= DAYS[-61])
    assert len(peer.read_code("600001").cells) < 61


@pytest.mark.parametrize("code", ("123", "../000001", "ABCDEF"))
def test_invalid_code_creates_no_database(tmp_path, code):
    cache = SQLiteQfqWindowCache(tmp_path / "daily_window.sqlite3", "v2", sqlite_policy=SqlitePolicyFixture())
    with pytest.raises(ValueError):
        cache.read_code(code)
    assert not cache.path.exists()


@pytest.mark.parametrize("corrupt_price", (999, b"invalid"))
def test_corrupt_prices_fail_closed(tmp_path, corrupt_price):
    cache = SQLiteQfqWindowCache(tmp_path / "daily_window.sqlite3", "v2", sqlite_policy=SqlitePolicyFixture())
    publish_windows(cache, (_window(),), "fixture")
    with SqliteSession(cache.path) as connection, SqliteTransaction(connection):
        connection.run("UPDATE daily_price SET qfq_close=? WHERE code='600001'", (corrupt_price,))
    with pytest.raises(RuntimeError, match="checksum_mismatch"):
        cache.read_code("600001")


@pytest.mark.parametrize("bound_publication", (False, True))
def test_projection_decode_releases_reader_before_concurrent_commit(tmp_path, monkeypatch, bound_publication):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from trader.download.infra import qfq_sqlite as module

    primary = SQLiteQfqWindowCache(tmp_path / "daily_window.sqlite3", "v2", sqlite_policy=SqlitePolicyFixture())
    peer = SQLiteQfqWindowCache(primary.path, "v3", sqlite_policy=SqlitePolicyFixture())
    publish_windows(primary, (_window(),), "old")
    manifest = primary.manifest()
    assert manifest is not None
    started, release = threading.Event(), threading.Event()
    decode = module.PriceCellFromColumns

    def blocked(*args):
        if not started.is_set():
            started.set()
            assert release.wait(10)
        return decode(*args)

    monkeypatch.setattr(module, "PriceCellFromColumns", blocked)
    with ThreadPoolExecutor(max_workers=2) as pool:
        reading = (
            pool.submit(primary.read_windows, manifest, ("600001",), sessions=251)
            if bound_publication
            else pool.submit(primary.read_code, "600001")
        )
        assert started.wait(3)
        publishing = pool.submit(
            primary.replace_paired_windows,
            peer,
            (_window(days=DAYS[-251:], price=12.0),),
            "new",
            QFQ_CONFIGURATION,
            calendar=DAYS[-251:],
            completed_day=DAYS[-1],
        )
        try:
            assert publishing.result(timeout=4)[1] == 251
        finally:
            release.set()
        if bound_publication:
            with pytest.raises(RuntimeError, match="qfq_publication_changed"):
                reading.result(timeout=5)
        else:
            assert reading.result(timeout=5).cells[-1].qfq.close_price == 10.0
    assert primary.read_code("600001").cells[-1].qfq.close_price == 12.0


def test_non_default_configuration_controls_supplier_and_both_published_windows(tmp_path) -> None:
    configuration = replace(QFQ_CONFIGURATION, v2_sessions=200, v3_sessions=50)
    supplier = Supplier(requested_sessions=200)
    updater = UpdateQfqWindows(
        SQLiteQfqWindowCache(tmp_path / "daily_window.sqlite3", "v2", sqlite_policy=SqlitePolicyFixture()),
        SQLiteQfqWindowCache(tmp_path / "daily_window.sqlite3", "v3", sqlite_policy=SqlitePolicyFixture()),
        supplier,
        lambda: False,
        lambda _message: None,
        configuration,
    )

    result = updater.execute(
        datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
    )

    assert result.completed_codes == 1
    assert supplier.calls == [("600001", DAYS[-200:])]
    assert len(updater.v2.read_code("600001").cells) == 200
    assert len(updater.v3.read_code("600001").cells) == 50
    v2_manifest = updater.v2.manifest()
    v3_manifest = updater.v3.manifest()
    assert v2_manifest is not None and len(v2_manifest.calendar_dates) == 200
    assert v3_manifest is not None and len(v3_manifest.calendar_dates) == 50


@pytest.mark.parametrize("cancel_during_write", (False, True))
def test_network_continues_while_writer_is_busy_and_shutdown_waits_for_commit(
    tmp_path, monkeypatch, cancel_during_write
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from trader.infra.workers import BoundedExecutor

    writing, release, downloaded, cancelled = Event(), Event(), Event(), Event()

    class RollingSupplier(Supplier):
        def fetch_window(self, security, dates):
            result = super().fetch_window(security, dates)
            if len(self.calls) >= 24:
                downloaded.set()
            return result

    supplier = RollingSupplier(codes=tuple(str(600001 + index) for index in range(40)))
    updater = _updater(tmp_path, supplier)
    commit = updater.v2.replace_paired_windows

    def blocked_commit(*args, **kwargs):
        writing.set()
        assert release.wait(10)
        return commit(*args, **kwargs)

    monkeypatch.setattr(updater.v2, "replace_paired_windows", blocked_commit)
    network = BoundedExecutor(worker_count=8, queue_capacity=0, thread_name_prefix="test-qfq-network")
    writer = BoundedExecutor(worker_count=1, queue_capacity=0, thread_name_prefix="test-qfq-writer")
    network.start()
    writer.start()
    updater = replace(updater, worker_pool=network, writer_pool=writer, cancel_requested=cancelled.is_set)
    observed = datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
    try:
        with ThreadPoolExecutor(max_workers=1) as coordinator:
            future = coordinator.submit(updater.execute, observed)
            try:
                assert writing.wait(5)
                assert downloaded.wait(5)
                assert len(supplier.calls) <= 32
                assert not (tmp_path / ".checkpoint.json").exists()
                if cancel_during_write:
                    cancelled.set()
                assert not future.done()
            finally:
                release.set()
            result = future.result(timeout=10)
            if cancel_during_write:
                assert result.failure_reason == "cancelled"
            else:
                assert result.completed_codes == 40 and not result.needs_retry
    finally:
        release.set()
        assert writer.stop(wait=True, cancel_futures=True).completed
        assert network.stop(wait=True, cancel_futures=True).completed


def test_inferred_suspension_keeps_only_tencent_real_bars_without_backfill(tmp_path):
    class SuspendedSupplier(Supplier):
        def fetch_window(self, security, dates):
            window = super().fetch_window(security, dates)
            return replace(
                window,
                cells=tuple(
                    replace(cell, status="tencent_inferred_suspended", unadjusted=None, qfq=None)
                    if cell.trade_date == DAYS[-2]
                    else cell
                    for cell in window.cells
                ),
            )

    supplier = SuspendedSupplier(days=DAYS)
    updater = _updater(tmp_path, supplier)
    observed = datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
    result = updater.execute(observed)
    assert result.completed_codes == 1 and result.pending_codes == 0
    assert len(supplier.calls) == 1
    assert DAYS[-2] not in {cell.trade_date for cell in updater.v2.read_code("600001").cells}
    assert len(updater.v2.read_code("600001").cells) == 250
    assert len(updater.v3.read_code("600001").cells) == 60
    projection = PublishedHistoryCache(
        ReadPublishedHistoryUseCase(updater.v2),
        lookback_sessions=251,
        open_dates=lambda: DAYS,
        monotonic=time.monotonic,
        projection_owner=SharedHistoryProjection(ReadPublishedHistoryUseCase(updater.v2), monotonic=time.monotonic, settings=HistoryPreloadSettings(warn_after_seconds=300.0)),
    )
    projection.initialize(cancelled=lambda: False)
    assert "600001" in projection.load(("600001",), observed_at=observed)
    resumed = updater.execute(observed)
    assert resumed.skipped_codes == 1 and resumed.pending_codes == 0
    assert len(supplier.calls) == 1


@pytest.mark.parametrize("whole_window", (False, True))
def test_confirmed_suspensions_complete_without_missing_price_or_retry(tmp_path, whole_window):
    class SuspendedSupplier(Supplier):
        def fetch_window(self, security, dates):
            window = super().fetch_window(security, dates)
            return replace(
                window,
                cells=tuple(
                    replace(
                        cell,
                        status="supplier_marked_suspended",
                        unadjusted=replace(cell.unadjusted, trading_status="suspended"),
                        qfq=replace(cell.qfq, trading_status="suspended"),
                    ) if whole_window or cell.trade_date == DAYS[-1] else cell
                    for cell in window.cells
                ),
            )

    supplier = SuspendedSupplier(days=DAYS)
    updater = _updater(tmp_path, supplier)
    observed = datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
    result = updater.execute(observed)
    assert result.completed_codes == 1 and result.pending_codes == 0
    assert result.latest_suspended_codes == 1
    assert result.latest_inferred_suspended_codes == result.latest_missing_codes == 0
    assert not result.needs_retry
    assert DAYS[-1] in updater.v2.covered_dates("600001")
    assert updater.v2.price_issues() == ()
    prices = updater.v2.read_code("600001").cells
    assert len(prices) == (0 if whole_window else 250)
    assert all(cell.status == "complete" for cell in prices)


def test_completed_checkpoint_preserves_inferred_latest_until_review_is_due(tmp_path) -> None:
    class LateDailySupplier(Supplier):
        latest_missing = True

        def fetch_window(self, security, dates):
            window = super().fetch_window(security, dates)
            if not self.latest_missing:
                return window
            return replace(
                window,
                cells=tuple(
                    replace(cell, status="tencent_inferred_suspended", unadjusted=None, qfq=None)
                    if cell.trade_date == DAYS[-1]
                    else cell
                    for cell in window.cells
                ),
            )

    supplier = LateDailySupplier(days=DAYS)
    updater = _updater(tmp_path, supplier)
    observed = datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
    first = updater.execute(observed)
    assert first.completed_codes == 1 and first.pending_codes == 0
    assert first.latest_missing_codes == 0
    assert first.latest_inferred_suspended_codes == 1
    assert not first.needs_retry
    source = updater.v2.inspect_windows(frozenset(("600001",))).states[0].source
    assert updater.v2.completed(DAYS[-1], "600001", source)
    assert updater.v2.read_code("600001").cells[-1].trade_date == DAYS[-2]
    next_morning = datetime.combine(DAYS[-1] + timedelta(days=1), datetime.min.time(), tzinfo=SHANGHAI).replace(
        hour=9, minute=30
    )
    projection = PublishedHistoryCache(
        ReadPublishedHistoryUseCase(updater.v2),
        lookback_sessions=251,
        open_dates=lambda: (*DAYS, next_morning.date()),
        monotonic=time.monotonic,
        projection_owner=SharedHistoryProjection(ReadPublishedHistoryUseCase(updater.v2), monotonic=time.monotonic, settings=HistoryPreloadSettings(warn_after_seconds=300.0)),
    )
    projection.initialize(cancelled=lambda: False)
    assert not projection.cached(("600001",), fresh_only=True, observed_at=next_morning)

    supplier.latest_missing = False
    calls = len(supplier.calls)
    second = updater.execute(observed)
    assert second.completed_codes == 1 and second.skipped_codes == 1
    assert second.latest_missing_codes == 0
    assert second.latest_inferred_suspended_codes == 1
    assert not second.price_review.discovery_completed
    assert second.changed_rows == 0 and len(supplier.calls) == calls
    assert updater.v2.read_code("600001").cells[-1].trade_date == DAYS[-2]
    assert updater.v3.read_code("600001").cells[-1].trade_date == DAYS[-2]
    projection.initialize(cancelled=lambda: False)
    assert not projection.cached(("600001",), fresh_only=True, observed_at=next_morning)


def test_suspension_conflict_preserves_the_previously_published_trading_prices(tmp_path):
    class ConflictSupplier(Supplier):
        conflict = False

        def fetch_window(self, security, dates):
            window = super().fetch_window(security, dates)
            if not self.conflict:
                return window
            return replace(
                window,
                cells=tuple(
                    replace(
                        cell,
                        status="supplier_marked_suspended",
                        unadjusted=replace(cell.unadjusted, trading_status="suspended"),
                        qfq=replace(cell.qfq, trading_status="suspended"),
                    ) for cell in window.cells
                ),
            )

    supplier = ConflictSupplier(days=DAYS[:-1])
    updater = _updater(tmp_path, supplier)
    observed = datetime.combine(DAYS[-2], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
    assert updater.execute(observed).completed_codes == 1
    previous = updater.v2.read_code("600001")
    supplier.days = DAYS
    supplier.conflict = True
    result = updater.execute(observed + timedelta(days=1))
    assert result.pending_codes == 1
    assert QfqPendingReason("price_suspension_trading_conflict", 1) in result.pending_reasons
    assert updater.v2.read_code("600001") == previous


def test_all_inferred_suspension_is_reported_without_empty_window_or_retry(tmp_path):
    class SuspendedSupplier(Supplier):
        def fetch_window(self, security, dates):
            window = super().fetch_window(security, dates)
            return replace(
                window,
                cells=tuple(
                    replace(cell, status="tencent_inferred_suspended", unadjusted=None, qfq=None)
                    for cell in window.cells
                ),
            )

    messages = []
    supplier = SuspendedSupplier(days=DAYS)
    updater = replace(
        _updater(tmp_path, supplier),
        report=lambda message: messages.append(_plain_feedback(message)),
    )
    observed = datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)

    result = updater.execute(observed)

    assert result.no_trade_data_codes == 1
    assert result.completed_codes == result.pending_codes == 0
    assert result.changed_files == ()
    assert updater.v2.read_code("600001").cells == updater.v3.read_code("600001").cells == ()
    assert any("结果 无真实日线" in message and "完整配对 0/0" in message for message in messages)
    assert any("无真实日线 1" in message and "待重试 0" in message for message in messages)
    assert updater.execute(observed).no_trade_data_codes == 1
    assert len(supplier.calls) == 2


def test_unpaired_only_window_does_not_report_no_trade_data_or_success(tmp_path):
    class UnpairedSupplier(Supplier):
        def fetch_window(self, security, dates):
            window = super().fetch_window(security, dates)
            return replace(window, cells=tuple(replace(cell, status="qfq_missing", qfq=None) for cell in window.cells))

    result = _updater(tmp_path, UnpairedSupplier(days=DAYS)).execute(
        datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
    )
    assert result.no_trade_data_codes == result.completed_codes == 0
    assert result.pending_codes == 1
    assert [(item.reason, item.count) for item in result.pending_reasons] == [("tencent_history_pending", 1)]


@pytest.mark.parametrize("profile", ("v2", "v3"))
@pytest.mark.parametrize(
    "field,value",
    (
        ("content_hash", bytes(32)),
        ("start_date", EncodeDate(date(2099, 1, 1))),
        ("end_date", EncodeDate(date(2000, 1, 1))),
    ),
)
def test_modified_database_is_rejected_before_supplier_calls(tmp_path, profile, field, value):
    supplier = Supplier()
    updater = _updater(tmp_path, supplier)
    observed = datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
    assert updater.execute(observed).completed_codes == 1
    cache = updater.v2 if profile == "v2" else updater.v3
    with SqliteSession(cache.path) as connection, SqliteTransaction(connection):
        connection.run(f"UPDATE window_binding SET {field}=? WHERE code=? AND profile=?", (value, "600001", profile))
    supplier.calls.clear()
    with pytest.raises(RuntimeError, match="checksum_mismatch"):
        updater.execute(observed)
    assert supplier.calls == []


def test_unchanged_eligibility_does_not_scan_price_rows(tmp_path, monkeypatch):
    cache = SQLiteQfqWindowCache((tmp_path) / "daily_window.sqlite3", "v2", sqlite_policy=SqlitePolicyFixture())
    publish_windows(cache, (_window("600001"), _window("600002")), "fixture")
    read = cache._connection
    statements = []

    def traced(**kwargs):
        connection = read(**kwargs)
        connection.observe_statements(statements.append)
        return connection

    monkeypatch.setattr(cache, "_connection", traced)
    peer = SQLiteQfqWindowCache(cache.path, "v3", sqlite_policy=SqlitePolicyFixture())
    before = _fingerprints(cache.path.parent)
    assert cache.select_published_codes(peer, frozenset(("600001", "600002"))) == ((), 0)
    assert _fingerprints(cache.path.parent) == before
    assert not any("daily_price" in sql for sql in statements)
    assert cache.select_published_codes(peer, frozenset(("600001",))) == (("daily_window.sqlite3",), 0)
    assert cache.codes() == frozenset(("600001",))
    assert len(cache.read_code("600001").cells) == 251
    assert len(cache.read_code("600002").cells) == 251
    assert len(peer.read_code("600002").cells) == 61


def test_window_inspection_only_reads_dates_and_reuses_file_verification(tmp_path, monkeypatch):
    cache = SQLiteQfqWindowCache(tmp_path / "daily_window.sqlite3", "v2", sqlite_policy=SqlitePolicyFixture())
    publish_windows(cache, (_window(),), "fixture")
    cache.verify_integrity()
    from trader.infra import sqlite_publication

    def unexpected_hash(*_args):
        pytest.fail("unchanged window inspection repeated a file hash")

    monkeypatch.setattr(sqlite_publication, "_digest", unexpected_hash)
    connect = cache._connection
    statements = []

    def traced(**kwargs):
        connection = connect(**kwargs)
        connection.observe_statements(statements.append)
        return connection

    monkeypatch.setattr(cache, "_connection", traced)
    first = cache.inspect_windows(frozenset({"600001"}))
    assert first == cache.inspect_windows(frozenset({"600001"}))
    assert first.states[0].dates == DAYS[-QFQ_CONFIGURATION.v2_sessions:]
    reads = [sql for sql in statements if "FROM daily_price" in sql]
    assert reads and all(sql.startswith("SELECT trade_date FROM daily_price ") for sql in reads)


def test_existing_window_without_receipt_is_rejected_before_republishing(tmp_path):
    updater = _updater(tmp_path, Supplier())
    publish_windows(updater.v2, (_window(),), "fixture")
    receipt = Path(f"{updater.v2.path}.sha256")
    receipt.unlink()
    observed = datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)

    with pytest.raises(RuntimeError, match="file_missing"):
        updater.execute(observed)

    assert not receipt.exists()


@pytest.mark.parametrize("status", ("unknown", "ever_st"))
def test_market_eligibility_preserves_unknown_and_retains_excluded_window(tmp_path, status):
    class Qualified(Supplier):
        reference = _reference(("600001", "600002"))

        def load_qfq_context(self, as_of, sessions):
            return replace(super().load_qfq_context(as_of, sessions), universe=self.reference.eligible_universe)

    supplier = Qualified(codes=("600001", "600002"))
    updater = _updater(tmp_path, supplier)
    now = datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
    assert updater.execute(now).completed_codes == 2
    before = updater.v2.read_code("600002"), updater.v3.read_code("600002")
    source = updater.v2.source_identity("600002")
    supplier.reference = replace(
        supplier.reference,
        st_evidence=tuple(
            replace(item, status=status) if item.code == "600002" else item
            for item in supplier.reference.st_evidence
        ),
    )
    supplier.calls.clear()
    result = updater.execute(now)
    allowed = frozenset({"600001", "600002"} if status == "unknown" else {"600001"})
    assert result.completed_codes == len(allowed) and result.pending_codes == 0
    assert supplier.calls == []
    assert updater.v2.codes() == updater.v3.codes() == allowed
    assert (updater.v2.read_code("600002"), updater.v3.read_code("600002")) == before
    assert updater.v2.completed(DAYS[-1], "600002", source)
    if status == "unknown":
        supplier.reference = _reference(("600001", "600002"))
        result = updater.execute(now)
        assert result.completed_codes == result.skipped_codes == 2
        assert supplier.calls == []


def test_explicit_market_sync_rejects_incomplete_database_without_restoring_tables(tmp_path):
    class Qualified(Supplier):
        def load_qfq_context(self, as_of, sessions):
            return replace(super().load_qfq_context(as_of, sessions), universe=_reference(("600001",)).eligible_universe)

    supplier = Qualified(codes=("600001",))
    updater = _updater(tmp_path, supplier)
    now = datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
    source = supplier.load_qfq_context(DAYS[-1], 251).source_versions
    identity = f"{source.sdk_version}:{source.content_hash}"
    publish_windows(updater.v2, (_window("600001"), _window("600002")), identity)
    with SqliteSession(updater.v2.path) as connection, SqliteTransaction(connection):
        connection.run("DROP TABLE window_population")
    updater.v2.publish_file()
    before = updater.v2.path.read_bytes()
    with pytest.raises(RuntimeError, match="qfq_manifest_unavailable"):
        updater.v2.manifest()
    assert updater.v2.path.read_bytes() == before
    with pytest.raises(sqlite3.DatabaseError, match="schema_unsupported"):
        updater.execute(now)
    assert updater.v2.path.read_bytes() == before
    assert supplier.calls == []


def test_qfq_refills_worker_before_slowest_request_finishes(tmp_path) -> None:
    import threading
    from concurrent.futures import ThreadPoolExecutor

    third_started = threading.Event()

    class SlowFirstSupplier(Supplier):
        def fetch_window(self, security, dates):
            if security.code == "600001":
                assert third_started.wait(5), "next stock was blocked by the slowest wave member"
            if security.code == "600003":
                third_started.set()
            return super().fetch_window(security, dates)

    supplier = SlowFirstSupplier(codes=("600001", "600002", "600003"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        updater = replace(_updater(tmp_path, supplier), workers=2, worker_pool=pool)
        result = updater.execute(datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16))
    assert third_started.is_set()
    assert result.completed_codes == 3 and result.pending_codes == 0


def test_qfq_transient_worker_rejection_does_not_leave_a_stock_pending(tmp_path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    class RejectOnce:
        def __init__(self, delegate):
            self.delegate = delegate
            self.rejections = 0

        def submit(self, function, /, *args, **kwargs):
            if not self.rejections:
                self.rejections += 1
                return None
            return self.delegate.submit(function, *args, **kwargs)

    supplier = Supplier(codes=("600001", "600002"))
    observed = datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
    with ThreadPoolExecutor(max_workers=2) as pool:
        worker = RejectOnce(pool)
        updater = replace(_updater(tmp_path, supplier), workers=2, worker_pool=worker)
        result = updater.execute(observed)
    assert worker.rejections == 1
    assert result.completed_codes == 2 and result.pending_codes == 0
    assert not result.pending_reasons
    assert len(updater.v2.read_code("600001").cells) == 251
    assert len(updater.v3.read_code("600002").cells) == 61


def test_permanent_worker_rejection_stops_after_one_capacity_deadline(tmp_path) -> None:
    import time

    class RejectAll:
        def __init__(self):
            self.codes = set()

        def submit(self, function, /, *args, **kwargs):
            self.codes.add(args[0].security.code)
            return None

    supplier = Supplier(codes=("600001", "600002", "600003"))
    worker = RejectAll()
    updater = replace(_updater(tmp_path, supplier), workers=3, worker_pool=worker)
    observed = datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
    started = time.monotonic()
    result = updater.execute(observed)
    assert time.monotonic() - started >= 4.5
    assert worker.codes == {"600001"}
    assert not supplier.calls
    assert result.completed_codes == 0 and result.pending_codes == 3
    assert result.failure_reason == "qfq_worker_unavailable"
    assert {item.reason: item.count for item in result.pending_reasons} == {
        "qfq_worker_unavailable": 1,
        "worker_unavailable_before_processing": 2,
    }
    assert not result.changed_files
    assert not (tmp_path / ".checkpoint.json").exists()


def test_worker_rejection_preserves_accepted_results_and_retry_checkpoint(tmp_path, monkeypatch) -> None:
    from concurrent.futures import Future

    monkeypatch.setattr("trader.infra.workers._CAPACITY_WAIT_SECONDS", 0)

    class AcceptFirst:
        def __init__(self):
            self.codes = []

        def submit(self, function, /, *args, **kwargs):
            self.codes.append(args[0].security.code)
            if len(self.codes) != 1:
                return None
            future = Future()
            future.set_result(function(*args, **kwargs))
            return future

    supplier = Supplier(codes=("600001", "600002", "600003"))
    worker = AcceptFirst()
    updater = replace(_updater(tmp_path, supplier), workers=3, worker_pool=worker)
    observed = datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
    result = updater.execute(observed)
    assert worker.codes == ["600001", "600002"]
    assert result.completed_codes == 1 and result.pending_codes == 2
    assert result.failure_reason == "qfq_worker_unavailable"
    assert len(updater.v2.read_code("600001").cells) == 251
    assert len(updater.v3.read_code("600001").cells) == 61
    assert {item.reason: item.count for item in result.pending_reasons} == {
        "qfq_worker_unavailable": 1,
        "worker_unavailable_before_processing": 1,
    }
    retry = _updater(tmp_path, supplier).execute(observed)
    assert retry.completed_codes == 3 and retry.skipped_codes == 1
    assert retry.pending_codes == 0 and retry.failure_reason is None
    assert len(supplier.calls) == 3


def test_one_serial_fetch_fills_both_profiles_and_restart_skips_completed(tmp_path) -> None:
    supplier = Supplier(codes=("600001", "300001"))
    updater = _updater(tmp_path, supplier)
    observed = datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
    result = updater.execute(observed)
    assert result.completed_codes == 2 and result.pending_codes == 0
    assert len(supplier.calls) == 2
    assert len(updater.v2.read_code("600001").cells) == 251
    assert len(updater.v3.read_code("600001").cells) == 61
    before = _fingerprints(tmp_path)
    result = _updater(tmp_path, supplier).execute(observed)
    assert result.skipped_codes == 2 and result.changed_files == ()
    assert len(supplier.calls) == 2
    assert _fingerprints(tmp_path) == before


def test_302_stock_download_publication_and_restart(tmp_path) -> None:
    class Tencent(Supplier):
        def load_qfq_context(self, as_of, sessions):
            context = super().load_qfq_context(as_of, sessions)
            return replace(context, universe=tuple(replace(security, board="chinext") for security in context.universe))

    supplier = Tencent(codes=("302132",))
    versions = supplier.load_qfq_context(DAYS[-1], 251).source_versions
    expected_source = f"{versions.sdk_version}:{versions.content_hash}"
    logs = []
    updater = replace(_updater(tmp_path, supplier), report=lambda message: logs.append(_plain_feedback(message)))
    observed = datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
    result = updater.execute(observed)
    assert result.completed_codes == 1 and result.pending_codes == 0 and result.failure_reason is None
    assert len(supplier.calls) == 1
    assert any("处理 Tencent下载 | 结果 合格" in line for line in logs)
    assert result.changed_files == ("daily_window.sqlite3",)
    for cache in (updater.v2, updater.v3):
        assert cache.codes() == frozenset({"302132"})
        manifest = cache.manifest()
        assert manifest is not None
        sessions = QFQ_CONFIGURATION.sessions_for(cache.profile)
        assert cache.read_windows(manifest, ("302132",), sessions=sessions) == (
            _window("302132", DAYS[-sessions:]),
        )
        assert cache.source_identity("302132") == expected_source
    resumed = updater.execute(observed)
    assert resumed.skipped_codes == 1 and resumed.pending_codes == 0 and resumed.changed_files == ()
    assert len(supplier.calls) == 1


@pytest.mark.parametrize("sessions", (33, 251))
def test_tencent_decimal_zero_evidence_is_published_and_reused(tmp_path, sessions) -> None:
    calls = []

    class Http:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def get(self, _url, **kwargs):
            param = kwargs["params"]["param"]
            calls.append(param)
            symbol = param.split(",")[0]
            rows = (
                [[day.isoformat()] for day in DAYS[1:]]
                if symbol == "sh000001"
                else [
                    [day.isoformat(), "10", "10.5", "11", "9.5", "100", {}, "1.2", "20", "0.00", "0.00"]
                    for day in DAYS[-sessions:]
                ]
            )
            # Tencent uses day even for qfq requests when there is no adjustment record.
            return SimpleNamespace(
                text=json.dumps({"code": 0, "data": {symbol: {"day": rows}}}),
                raise_for_status=lambda: None,
                close=lambda: None,
            )

    security = BaoStockSecurity("688001", "fixture", "star", DAYS[-sessions], None, "fixture")
    supplier = TencentQfqSupplier(
        TencentQfqDependencies(Http, lambda _as_of: (security,), lambda: False),
        QFQ_TENCENT_OPTIONS,
    )
    logs = []
    updater = UpdateQfqWindows(
        SQLiteQfqWindowCache((tmp_path) / "daily_window.sqlite3", "v2", sqlite_policy=SqlitePolicyFixture()),
        SQLiteQfqWindowCache((tmp_path) / "daily_window.sqlite3", "v3", sqlite_policy=SqlitePolicyFixture()),
        supplier,
        lambda: False,
        lambda message: logs.append(_plain_feedback(message)),
        QFQ_CONFIGURATION,
    )
    observed = datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
    result = updater.execute(observed)
    assert result.completed_codes == 1 and result.pending_codes == 0
    assert len(updater.v2.read_code(security.code).cells) == sessions
    assert len(updater.v3.read_code(security.code).cells) == min(sessions, 61)
    assert any("合格 1 | 待重试 0" in message for message in logs)
    assert len(calls) == 3
    resumed = updater.execute(observed)
    assert resumed.skipped_codes == 1 and resumed.pending_codes == 0
    assert resumed.changed_files == () and resumed.changed_rows == 0
    assert len(calls) == 4  # Calendar refresh only; completed stock is not downloaded again.


def test_incomplete_supplier_gap_preserves_only_returned_pairs(tmp_path) -> None:
    supplier = Supplier()
    original = supplier.fetch_window

    def incomplete(security, dates):
        window = original(security, dates)
        missing = dates[-2:]
        return PublishedHistoryWindow(
            security.code,
            tuple(
                replace(cell, qfq=None, status="qfq_missing") if cell.trade_date in missing else cell
                for cell in window.cells
            ),
        )

    supplier.fetch_window = incomplete
    logs = []
    updater = UpdateQfqWindows(
        SQLiteQfqWindowCache((tmp_path) / "daily_window.sqlite3", "v2", sqlite_policy=SqlitePolicyFixture()),
        SQLiteQfqWindowCache((tmp_path) / "daily_window.sqlite3", "v3", sqlite_policy=SqlitePolicyFixture()),
        supplier,
        lambda: False,
        lambda message: logs.append(_plain_feedback(message)),
        QFQ_CONFIGURATION,
    )
    observed = datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
    result = updater.execute(observed)
    assert result.completed_codes == 0 and result.pending_codes == 1
    assert updater.v2.read_code("600001").cells == ()
    assert updater.v3.read_code("600001").cells == ()
    assert any("结果 待重试" in message for message in logs)


def test_all_tencent_waves_keep_incomplete_stocks_pending_without_fallback(tmp_path) -> None:
    import threading

    from trader.infra.workers import BoundedExecutor

    events = []
    barrier = threading.Barrier(2)

    class Tencent(Supplier):
        def fetch_window(self, security, dates):
            barrier.wait(timeout=3)
            events.append(("tencent", security.code))
            window = super().fetch_window(security, dates)
            if security.code == "600004":
                raise QfqWindowIncompleteError(dates, dates)
            if security.code == "600001":
                return replace(
                    window,
                    cells=(
                        replace(window.cells[0], status="unknown_missing", unadjusted=None, qfq=None),
                        *window.cells[1:],
                    ),
                )
            return window

    logs = []
    supplier = Tencent(codes=("600001", "600002", "600003", "600004"))
    pool = BoundedExecutor(worker_count=2, queue_capacity=0, thread_name_prefix="qfq-recovery-test")
    pool.start()
    try:
        updater = replace(
            _updater(tmp_path, supplier),
            workers=2,
            worker_pool=pool,
            report=lambda message: logs.append(_plain_feedback(message)),
        )
        observed = datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
        result = updater.execute(observed)
        assert result.completed_codes == 2 and result.pending_codes == 2
        assert [event[0] for event in events] == ["tencent"] * 4
        assert sum(" | 股票 " in line and " | 结果 " in line for line in logs) == 4
        assert any("已处理 4/4（100.00%） | 合格 2 | 待重试 2" in line for line in logs)
        for cache in (updater.v2, updater.v3):
            assert cache.codes() == frozenset({"600002", "600003"})
        before = _fingerprints(tmp_path)
        resumed = updater.execute(observed)
        assert resumed.skipped_codes == 2 and resumed.pending_codes == 2 and resumed.changed_files == ()
        assert len(events) == 6
        assert _fingerprints(tmp_path) == before
    finally:
        assert pool.stop(wait=True, cancel_futures=True).completed


def test_new_day_retries_incomplete_stock_with_tencent(tmp_path) -> None:
    supplier = Supplier(days=DAYS[:-1], fail=True)
    updater = _updater(tmp_path, supplier)
    observed = datetime.combine(DAYS[-2], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
    first = updater.execute(observed)
    assert first.completed_codes == 0 and first.pending_codes == 1
    assert updater.v2.read_code("600001").cells == ()
    supplier.fail = False
    supplier.days = DAYS[1:]
    supplier.calls.clear()
    result = updater.execute(observed + timedelta(days=1))
    assert result.completed_codes == 1 and result.pending_codes == 0
    assert supplier.calls == [("600001", DAYS[1:])]
    assert updater.v2.source_identity("600001").startswith("fixture:")
    assert all(cell.qfq.close_price == 10 for cell in updater.v2.read_code("600001").cells)


@pytest.mark.parametrize("conflict", (False, True))
def test_small_tail_exact_overlap_or_bounded_revision_refetch(tmp_path, conflict) -> None:
    supplier = Supplier(conflict=conflict)
    updater = _updater(tmp_path, supplier)
    context = supplier.load_qfq_context(DAYS[-1], 251)
    source = f"fixture:{context.source_versions.content_hash}"
    publish_windows(updater.v2, (_window(days=DAYS[:-1]),), source)
    result = updater.execute(datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16))
    assert result.pending_codes == 0
    assert supplier.calls[0][1] == DAYS[-4:]
    assert len(supplier.calls) == (2 if conflict else 1)
    if conflict:
        assert supplier.calls[1][1] == DAYS[-251:]
    assert tuple(cell.trade_date for cell in updater.v2.read_code("600001").cells) == DAYS[-251:]


def test_same_day_without_checkpoint_only_rechecks_three_overlap_dates(tmp_path) -> None:
    supplier = Supplier()
    updater = _updater(tmp_path, supplier)
    context = supplier.load_qfq_context(DAYS[-1], 251)
    source = f"fixture:{context.source_versions.content_hash}"
    publish_windows(updater.v2, (_window(),), source)
    result = updater.execute(datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16))
    assert result.changed_files == ("daily_window.sqlite3",)  # Completion is committed with the unchanged prices.
    assert result.changed_rows == 0
    assert supplier.calls == [("600001", DAYS[-3:])]


def test_interrupted_between_profiles_does_not_advance_checkpoint_and_retry_repairs(tmp_path, monkeypatch) -> None:
    supplier = Supplier()
    updater = _updater(tmp_path, supplier)
    from trader.infra.sqlite_connection import SqliteConnection

    write = SqliteConnection.run

    def fail(connection, sql, parameters=()):
        if sql.startswith("INSERT OR REPLACE INTO window_binding") and parameters[0] == "v3":
            raise RuntimeError("interrupted second profile")
        return write(connection, sql, parameters)

    monkeypatch.setattr(SqliteConnection, "run", fail)
    observed = datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
    result = updater.execute(observed)
    assert result.pending_codes == 1
    source = f"fixture:{supplier.load_qfq_context(DAYS[-1], 251).source_versions.content_hash}"
    assert not updater.v2.completed(DAYS[-1], "600001", source)
    assert updater.v2.read_code("600001").cells == ()
    assert updater.v3.read_code("600001").cells == ()
    restarted = SQLiteQfqWindowCache(updater.v2.path, "v2", sqlite_policy=SqlitePolicyFixture())
    restarted.verify_integrity()
    assert restarted.manifest() is None
    assert restarted.codes() == frozenset()
    assert restarted.source_identity("600001") is None
    assert restarted.inspect_windows(frozenset({"600001"})).states == ()
    with SqliteSession(restarted.path) as connection:
        assert connection.run("PRAGMA user_version").require_one()[0] == 0
        assert connection.run("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
    monkeypatch.setattr(SqliteConnection, "run", write)
    result = updater.execute(observed)
    assert result.pending_codes == 0 and result.completed_codes == 1
    assert len(updater.v3.read_code("600001").cells) == 61
    assert updater.v2.completed(DAYS[-1], "600001", source)


def test_source_migration_ignores_legacy_checkpoint_and_refetches_full_window(tmp_path):
    supplier = Supplier()
    updater = _updater(tmp_path, supplier)
    publish_windows(updater.v2, (_window(),), "history:seed")
    source = f"fixture:{supplier.load_qfq_context(DAYS[-1], 251).source_versions.content_hash}"
    # Even a valid same-day code checkpoint cannot relabel old-source rows.
    with SqliteSession(updater.v2.path) as connection, SqliteTransaction(connection):
        connection.run("INSERT INTO window_completion VALUES(?,?,?)", ("600001", EncodeDate(DAYS[-1]), source))
    updater.v2.publish_file()
    result = updater.execute(datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16))
    assert result.completed_codes == 1 and result.skipped_codes == 0
    assert supplier.calls == [("600001", DAYS[-251:])]
    assert updater.v2.source_identity("600001") == updater.v3.source_identity("600001") == source


def test_bounded_parallel_downloads_single_writer_and_cancelled_results_not_published(tmp_path, monkeypatch):
    import threading

    from trader.infra.workers import BoundedExecutor

    barrier = threading.Barrier(2)
    cancelled = threading.Event()
    writer_ident = threading.get_ident()

    class ParallelSupplier(Supplier):
        cancel_on_return = False

        def fetch_window(self, security, dates):
            barrier.wait(timeout=5)
            window = super().fetch_window(security, dates)
            if self.cancel_on_return:
                cancelled.set()
            return window

    supplier = ParallelSupplier(codes=("600001", "600002", "600003", "600004"))
    updater = _updater(tmp_path, supplier)
    original = updater.v2.replace_paired_windows

    def write(peer, windows, identity, configuration, **kwargs):
        assert threading.get_ident() == writer_ident
        return original(peer, windows, identity, configuration, **kwargs)

    monkeypatch.setattr(updater.v2, "replace_paired_windows", write)
    pool = BoundedExecutor(worker_count=2, queue_capacity=0, thread_name_prefix="qfq-fixture")
    pool.start()
    try:
        updater = replace(updater, workers=2, worker_pool=pool, cancel_requested=cancelled.is_set)
        observed = datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
        result = updater.execute(observed)
        assert result.completed_codes == 4 and pool.status().submitted_count == 4
        # A late successful return after cancellation must not create a new window or checkpoint.
        supplier.cancel_on_return = True
        supplier.codes = ("600005", "600006")
        result = updater.execute(observed)
        assert result.pending_codes == 2 and result.failure_reason == "cancelled"
        assert updater.v2.read_code("600005").cells == ()
    finally:
        assert pool.stop(wait=True, cancel_futures=True).completed


def test_one_unreadable_code_does_not_block_other_stocks(tmp_path, monkeypatch):
    supplier = Supplier(codes=("600001", "600002"))
    updater = _updater(tmp_path, supplier)
    context = supplier.load_qfq_context(DAYS[-1], 251)
    source = f"{context.source_versions.sdk_version}:{context.source_versions.content_hash}"
    publish_windows(updater.v2, (_window("600001"), _window("600002")), source)
    original = updater.v2.read_codes

    def read(codes):
        if codes == ("600001",):
            raise RuntimeError("corrupt shard")
        return original(codes)

    monkeypatch.setattr(updater.v2, "read_codes", read)
    updater = replace(updater, workers=1)
    result = updater.execute(datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16))
    assert result.pending_codes == 1 and result.completed_codes == 1
    assert len(updater.v3.read_code("600002").cells) == 61


def test_batch_progress_handles_out_of_order_completion_partial_batch_and_reuse(tmp_path, monkeypatch):
    import threading

    from trader.infra.workers import BoundedExecutor

    second_published = threading.Event()
    writer_ident = threading.get_ident()
    messages = []
    writes = []

    class OutOfOrderSupplier(Supplier):
        def fetch_window(self, security, dates):
            if security.code == "600001":
                assert second_published.wait(timeout=5)
            window = super().fetch_window(security, dates)
            if security.code == "600002":
                second_published.set()
            return window

    supplier = OutOfOrderSupplier(codes=("600001", "600002", "600003"))
    updater = _updater(tmp_path, supplier)
    original = updater.v2.replace_paired_windows

    def write(peer, windows, identity, configuration, **kwargs):
        result = original(peer, windows, identity, configuration, **kwargs)
        writes.extend(window.code for window in windows)
        return result

    def report(message):
        assert threading.get_ident() == writer_ident
        message = _plain_feedback(message)
        messages.append(message)

    monkeypatch.setattr(updater.v2, "replace_paired_windows", write)
    pool = BoundedExecutor(worker_count=2, queue_capacity=0, thread_name_prefix="qfq-progress-fixture")
    pool.start()
    try:
        updater = replace(updater, workers=2, worker_pool=pool, report=report)
        observed = datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
        result = updater.execute(observed)
        assert result.completed_codes == 3
        assert sorted(writes) == ["600001", "600002", "600003"]
        stock_progress = [message for message in messages if " | 已处理 " in message and " | 股票 " in message]
        assert len(stock_progress) == 3
        assert all("处理 Tencent下载 | 结果 合格" in message for message in stock_progress)
        assert all(
            "raw 251/251 | qfq 251/251 | 完整配对 251/251 | 缺日 0 | 原因 无" in message for message in stock_progress
        )
        assert "market·完成 | 完成 | " in messages[-1]
        ready = next(message for message in messages if "market·就绪 | 完成 | " in message)
        assert "股票 3 只" in ready and "并发 2" in ready

        messages.clear()
        resumed = updater.execute(observed)
        assert resumed.skipped_codes == 3 and resumed.changed_files == ()
        assert len(supplier.calls) == 3
        stock_progress = [message for message in messages if " | 已处理 " in message and " | 股票 " in message]
        assert len(stock_progress) == 3
        assert all("处理 本地复用 | 结果 复用" in message for message in stock_progress)
        assert "合格 3 | 待重试 0 | 其中复用 3" in messages[-1]
        assert "未处理 0" in messages[-1]
    finally:
        assert pool.stop(wait=True, cancel_futures=True).completed


def test_pending_progress_uses_one_detailed_stock_line_and_preserves_old_window(tmp_path):
    supplier = _IncompleteSupplier(codes=("600001", "600002"))
    messages = []
    updater = replace(_updater(tmp_path, supplier), report=lambda message: messages.append(_plain_feedback(message)))
    publish_windows(updater.v2, (_window(),), "old-source")
    before = updater.v2.read_code("600001")
    observed = datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
    result = updater.execute(observed)
    assert result.completed_codes == 1 and result.pending_codes == 1
    assert updater.v2.read_code("600001") == before
    stock_progress = [message for message in messages if " | 已处理 " in message and " | 股票 " in message]
    assert len(stock_progress) == 2
    pending = next(message for message in stock_progress if "600001 fixture" in message)
    assert "处理 Tencent下载 | 结果 待重试" in pending
    assert "raw 247/251 | qfq 247/251 | 完整配对 247/251 | 缺日 4 |" in pending
    assert " | 缺失日期 " in pending
    assert DAYS[1].isoformat() in pending
    assert " | 原因 qfq_incomplete_raw_4_qfq_4 | 缺失日期 " in pending
    assert "market·结束（仍有待重试） | 完成 | " in messages[-1]
    assert "market·待重试原因 | 处理中 | qfq_window_incomplete 1 股" in messages[-2]
    assert [(item.reason, item.count) for item in result.pending_reasons] == [("qfq_window_incomplete", 1)]
    assert result.failure_reason is None


def test_tencent_diagnostic_counts_pending_with_new_batch_feedback():
    from scripts.download.history.tencent_download import _benchmark

    supplier = _IncompleteSupplier(codes=("600001", "600002"))
    context = supplier.load_qfq_context(DAYS[-1], 251)
    observed = datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
    report = _benchmark(supplier, context, 2, 2, observed, sqlite_policy=SqlitePolicyFixture())
    assert report["completed"] == report["pending"] == 1
    assert report["failure_categories"] == {"qfq_incomplete_raw_4_qfq_4": 1}
    assert report["restart_failure_categories"] == report["failure_categories"]
    assert report["restart_skipped"] == 1 and report["restart_changed_files"] == 0


def test_reused_codes_do_not_reduce_actual_download_wave_size(tmp_path):
    supplier = Supplier(codes=("600001", "600003", "600005", "600007"))
    updater = replace(_updater(tmp_path, supplier), workers=2)
    observed = datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
    updater.execute(observed)
    supplier.codes = tuple(f"60000{index}" for index in range(1, 9))
    messages = []
    result = replace(updater, report=lambda message: messages.append(_plain_feedback(message))).execute(observed)
    stock_progress = [message for message in messages if " | 已处理 " in message and " | 股票 " in message]
    downloaded = [message for message in stock_progress if "处理 Tencent下载" in message]
    reused = [message for message in stock_progress if "处理 本地复用" in message]
    assert len(stock_progress) == 8 and len(downloaded) == len(reused) == 4
    assert {message.split(" | ")[3].split()[1] for message in downloaded} == {
        "600002",
        "600004",
        "600006",
        "600008",
    }
    assert result.completed_codes == 8 and result.skipped_codes == 4
    assert len(supplier.calls) == 8


def test_preparation_pending_is_reported_without_any_download_and_hides_payload(tmp_path, monkeypatch):
    supplier = Supplier()
    messages = []
    updater = replace(_updater(tmp_path, supplier), report=lambda message: messages.append(_plain_feedback(message)))

    context = supplier.load_qfq_context(DAYS[-1], 251)
    source = f"{context.source_versions.sdk_version}:{context.source_versions.content_hash}"
    publish_windows(updater.v2, (_window(),), source)

    def read(_codes):
        raise RuntimeError("private supplier payload\nhttps://example.invalid?token=secret")

    monkeypatch.setattr(updater.v2, "read_codes", read)
    observed = datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
    result = updater.execute(observed)
    assert result.completed_codes == 0 and result.pending_codes == 1
    assert supplier.calls == []
    assert "已处理 1/1（100.00%） | 合格 0 | 待重试 1" in messages[-1]
    details = next(message for message in messages if " | 已处理 " in message and " | 股票 " in message)
    assert "600001 fixture | 处理 本地检查 | 结果 失败" in details
    assert "raw -/251 | qfq -/251 | 完整配对 -/251 | 缺日 未知 | 原因 RuntimeError" in details
    assert "payload" not in "\n".join(messages) and "secret" not in "\n".join(messages)


def test_no_applicable_dates_are_counted_separately_and_elapsed_includes_context_loading(tmp_path):
    clock = [100.0]

    class FutureListingSupplier(Supplier):
        def load_qfq_context(self, as_of, sessions):
            context = super().load_qfq_context(as_of, sessions)
            clock[0] += 65
            return replace(
                context,
                universe=tuple(replace(item, listed_on=DAYS[-1] + timedelta(days=1)) for item in context.universe),
            )

    supplier = FutureListingSupplier()
    messages = []
    updater = replace(
        _updater(tmp_path, supplier),
        report=lambda message: messages.append(_plain_feedback(message)),
        monotonic=lambda: clock[0],
    )
    observed = datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
    result = updater.execute(observed)
    assert result.completed_codes == result.pending_codes == result.skipped_codes == 0
    assert supplier.calls == []
    assert messages[0].startswith("market·准备 | 开始 | ")
    stock_progress = next(message for message in messages if " | 已处理 " in message and " | 股票 " in message)
    assert "处理 无需下载 | 结果 无需下载 | raw 0/0 | qfq 0/0 | 完整配对 0/0" in stock_progress
    assert messages[-1].startswith("market·完成 | 完成 | 已处理 1/1（100.00%）")
    assert "合格 0 | 待重试 0 | 其中复用 0 | 无需下载 1 | 无真实日线 0" in messages[-1]
    assert "最近日停牌 0 | 最近日推定停牌 0 | 最近日未解释缺价 0 | 未处理 0" in messages[-1]


def test_cancellation_does_not_count_late_results_or_unvisited_stocks_as_processed(tmp_path):
    cancelled = False

    class CancelSupplier(Supplier):
        def fetch_window(self, security, dates):
            nonlocal cancelled
            window = super().fetch_window(security, dates)
            cancelled = True
            return window

    supplier = CancelSupplier(codes=("600001", "600002", "600003"))
    messages = []
    updater = replace(
        _updater(tmp_path, supplier),
        report=lambda message: messages.append(_plain_feedback(message)),
        workers=2,
        cancel_requested=lambda: cancelled,
    )
    observed = datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
    result = updater.execute(observed)
    assert result.failure_reason == "cancelled" and result.pending_codes == 3
    assert result.completed_codes == 0 and result.changed_files == ()
    assert len(supplier.calls) == 1
    assert "market·已取消 | 完成 | 已处理 0/3（0.00%） | 合格 0 | 待重试 0" in messages[-1]
    assert "未处理 3" in messages[-1]
    assert not any(" | 已处理 " in message and " | 股票 " in message for message in messages)


def test_context_failure_reports_bounded_reason_and_preparation_elapsed(tmp_path):
    clock = [100.0]

    class BrokenContextSupplier(Supplier):
        def load_qfq_context(self, as_of, sessions):
            clock[0] += 3
            raise RuntimeError("private supplier payload")

    messages = []
    updater = replace(
        _updater(tmp_path, BrokenContextSupplier()),
        report=lambda message: messages.append(_plain_feedback(message)),
        monotonic=lambda: clock[0],
    )
    with pytest.raises(RuntimeError, match="private supplier payload"):
        updater.execute(datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16))
    assert messages[-1] == "market·准备失败 | 失败 | RuntimeError"


def test_latest_day_missing_retries_only_when_the_batch_gap_is_widespread() -> None:
    assert not QfqUpdateResult(DAYS[-1], 4401, latest_missing_codes=44).needs_retry
    assert QfqUpdateResult(DAYS[-1], 4401, latest_missing_codes=45).needs_retry
    assert QfqUpdateResult(DAYS[-1], 100, latest_missing_codes=20).needs_retry
    assert not QfqUpdateResult(DAYS[-1], 100, latest_missing_codes=19).needs_retry


def test_online_projection_matches_full_history_and_keeps_settlement_independent(tmp_path) -> None:
    from tests.helpers.recommendation.market.candidate_history_tail import CALENDAR, NOW, HistoryInput

    class MissingTailHistoryInput(HistoryInput):
        @staticmethod
        def window(code, dates):
            window = HistoryInput.window(code, dates)
            return replace(window, cells=window.cells[:-2]) if code == "600002" else window

    full = MissingTailHistoryInput(251, 0, ("600001", "600002"))
    reader = ReadPublishedHistoryUseCase(full)
    calendar_calls = 0

    def dates():
        nonlocal calendar_calls
        calendar_calls += 1
        return CALENDAR

    projection = PublishedHistoryCache(
        reader,
        lookback_sessions=251,
        outcome_history=reader,
        open_dates=dates,
        monotonic=time.monotonic,
        projection_owner=SharedHistoryProjection(reader, monotonic=time.monotonic, settings=HistoryPreloadSettings(warn_after_seconds=300.0)),
    )
    original = PublishedHistoryCache(reader, lookback_sessions=251, monotonic=time.monotonic, outcome_history=reader, projection_owner=SharedHistoryProjection(reader, monotonic=time.monotonic, settings=HistoryPreloadSettings(warn_after_seconds=300.0)))
    projection.initialize(cancelled=lambda: False)
    original.initialize(cancelled=lambda: False)
    assert calendar_calls == 1  # Startup aligns the shared history projection to the calendar.
    restrictions = {}
    loaded = projection.load(("600001", "600002"), observed_at=NOW, action_restrictions=restrictions)
    assert tuple(loaded) == ("600001",)
    assert calendar_calls == 2  # Freshness is checked once for the whole requested batch.
    assert restrictions == {"600002": {"history_data_pending"}}
    expected = original.load(("600001",), observed_at=NOW)
    assert loaded == expected
    assert projection.summaries(loaded, NOW) == original.summaries(expected, NOW)
    missing = PublishedHistoryCache(
        ReadPublishedHistoryUseCase(SQLiteQfqWindowCache((tmp_path / "missing") / "daily_window.sqlite3", "v2", sqlite_policy=SqlitePolicyFixture())),
        lookback_sessions=251,
        outcome_history=reader,
        monotonic=time.monotonic,
        projection_owner=SharedHistoryProjection(ReadPublishedHistoryUseCase(SQLiteQfqWindowCache((tmp_path / "missing") / "daily_window.sqlite3", "v2", sqlite_policy=SqlitePolicyFixture())), monotonic=time.monotonic, settings=HistoryPreloadSettings(warn_after_seconds=300.0)),
    )
    missing.initialize(cancelled=lambda: False)
    assert missing.read_outcome_bars(("600001",), NOW) == original.read_outcome_bars(("600001",), NOW)
    assert missing.load(("600001",), observed_at=NOW) == {}


@pytest.mark.parametrize("suspension", ("inside", "boundary", "latest", "whole_peer"))
def test_market_publication_accepts_suspension_without_counting_price_rows(tmp_path, suspension):
    suspended_days = {
        "inside": (DAYS[-30],),
        "boundary": (DAYS[-61],),
        "latest": (DAYS[-1],),
        "whole_peer": DAYS[-61:],
    }[suspension]

    class SuspendedSupplier(Supplier):
        def fetch_window(self, security, dates):
            window = super().fetch_window(security, dates)
            return replace(window, cells=tuple(
                replace(cell, status="tencent_inferred_suspended", unadjusted=None, qfq=None)
                if cell.trade_date in suspended_days else cell
                for cell in window.cells
            ))

    supplier = SuspendedSupplier()
    messages = []
    updater = replace(_updater(tmp_path, supplier), report=lambda message: messages.append(_plain_feedback(message)))
    observed = datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)

    result = updater.execute(observed)

    assert result.failure_reason is None and not result.needs_retry
    assert result.completed_codes == 1 and result.pending_codes == 0
    assert len(updater.v2.read_code("600001").cells) == 251 - len(suspended_days)
    assert len(updater.v3.read_code("600001").cells) == 61 - len(suspended_days)
    assert any("数据库文件校验" in message and "完成" in message for message in messages)
    if suspension in ("inside", "boundary"):
        before = _fingerprints(tmp_path)
        resumed = updater.execute(observed)
        assert resumed.failure_reason is None and resumed.skipped_codes == 1
        assert len(supplier.calls) == 1
        assert _fingerprints(tmp_path) == before


def test_market_publication_uses_persisted_bounds_instead_of_current_configuration(tmp_path):
    updater = _updater(tmp_path, Supplier())
    published_configuration = replace(QFQ_CONFIGURATION, v3_sessions=50)
    updater.v2.replace_paired_windows(
        updater.v3, (_window(days=DAYS[1:]),), "fixture", published_configuration,
        calendar=DAYS[1:], completed_day=DAYS[-1],
    )
    before = _fingerprints(tmp_path)

    updater.v2.verify_integrity()
    assert len(updater.v3.read_code("600001").cells) == 50
    assert _fingerprints(tmp_path) == before


def test_market_publication_keeps_old_valid_pair_when_another_stock_advances(tmp_path):
    updater = _updater(tmp_path, _IncompleteSupplier(codes=("600001", "600002")))
    publish_windows(updater.v2, (_window(days=DAYS[:-1]),), "old-source")
    old_window = updater.v3.read_code("600001")
    observed = datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)

    result = updater.execute(observed)

    assert result.failure_reason is None
    assert result.completed_codes == 1 and result.pending_codes == 1 and result.needs_retry
    assert updater.v3.read_code("600001") == old_window
    assert updater.v3.read_code("600002").cells[-1].trade_date == DAYS[-1]


@pytest.mark.parametrize(
    "field,value,reason",
    (
        ("source", "other-source", "sqlite_publication_checksum_mismatch"),
        ("end_date", EncodeDate(DAYS[-1] + timedelta(days=1)), "sqlite_publication_checksum_mismatch"),
        ("content_hash", bytes(32), "sqlite_publication_checksum_mismatch"),
    ),
)
def test_market_publication_still_rejects_invalid_identity(tmp_path, field, value, reason):
    updater = _updater(tmp_path, Supplier())
    observed = datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
    assert updater.execute(observed).failure_reason is None
    with SqliteSession(updater.v2.path) as connection, SqliteTransaction(connection, immediate=True):
        connection.run(f"UPDATE window_binding SET {field}=? WHERE profile='v3'", (value,))

    with pytest.raises(RuntimeError, match=reason):
        updater.execute(observed)


def test_market_publication_check_rejects_corrupted_pair(tmp_path):
    updater = _updater(tmp_path, Supplier(codes=("600001",)))
    observed = datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
    result = updater.execute(observed)
    assert result.failure_reason is None
    with SqliteSession(tmp_path / "daily_window.sqlite3") as connection, SqliteTransaction(connection, immediate=True):
        connection.run("DELETE FROM window_binding WHERE profile='v3'")
    with pytest.raises(RuntimeError, match="checksum_mismatch"):
        updater.execute(observed)
    with pytest.raises(RuntimeError, match="checksum_mismatch"):
        updater.v2.verify_integrity()

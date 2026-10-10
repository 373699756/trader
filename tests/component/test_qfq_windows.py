from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from trader.download.application.read_published_history import ReadPublishedHistoryUseCase
from trader.download.application.update_qfq import UpdateQfqWindows
from trader.download.domain.baostock_daily import (
    BaoStockCalendar,
    BaoStockDailyCell,
    BaoStockDailySide,
    BaoStockSecurity,
    BaoStockSourceVersions,
)
from trader.download.domain.history_sync import HistorySupplierContext
from trader.download.domain.published_history import PublishedHistoryWindow, project_history_cell
from trader.download.domain.qfq_window import QfqUpdateResult, completed_daily_cutoff
from trader.download.infra.history_control_repository import HistoryMaintenanceLock
from trader.download.infra.qfq_checkpoint import QfqCheckpoint
from trader.download.infra.qfq_maintenance import QfqDailyMaintenance
from trader.download.infra.qfq_sqlite import SQLiteQfqWindowCache
from trader.download.infra.qfq_update_runner import QfqUpdateRunner
from trader.download.infra.tencent_qfq_supplier import TencentQfqDependencies, TencentQfqSupplier
from trader.entrypoints import cli
from trader.recommendation.infra.market_data.published_history_cache import PublishedHistoryCache

SHANGHAI = ZoneInfo("Asia/Shanghai")
DAYS = tuple(date(2025, 1, 1) + timedelta(days=offset) for offset in range(252))


def _cell(code: str, day: date, *, price: float = 10.0) -> BaoStockDailyCell:
    raw = BaoStockDailySide(
        code,
        day,
        "unadjusted",
        price,
        price + 0.2,
        price - 0.2,
        price,
        100000.0,
        10000000.0,
        price - 0.01,
        0.1,
        1.0,
        "trading",
    )
    qfq = replace(raw, adjustment="qfq", preclose=None, pct_change=None, turnover=None)
    return BaoStockDailyCell(code, day, "complete", raw, qfq)


def _window(code: str = "600001", days: tuple[date, ...] = DAYS, *, price: float = 10.0) -> PublishedHistoryWindow:
    return PublishedHistoryWindow(code, tuple(project_history_cell(_cell(code, day, price=price)) for day in days))


def _fingerprints(root: Path) -> dict[str, tuple[str, int]]:
    return {
        path.name: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
        for path in root.iterdir()
        if path.is_file()
    }


@pytest.mark.parametrize("profile,sessions", (("v2", 251), ("v3", 61)))
def test_sqlite_rolls_windows_and_noop_is_byte_identical(tmp_path, profile, sessions) -> None:
    cache = SQLiteQfqWindowCache(tmp_path, profile)
    changed, count = cache.replace_window(_window(days=DAYS[:-1]), "history:seed")
    assert changed == (f"{profile}/qfq_sse_main.sqlite3",)
    assert not (cache.root / "index.json").exists()
    assert count == sessions
    before = _fingerprints(cache.root)
    assert cache.replace_window(_window(days=DAYS[:-1]), "history:seed") == ((), 0)
    assert _fingerprints(cache.root) == before
    changed, count = cache.replace_window(_window(), "baostock:refresh")
    assert count == 2
    assert len(changed) == 1 and changed[0].endswith(".sqlite3")
    assert tuple(cell.trade_date for cell in cache.read_code("600001").cells) == DAYS[-sessions:]
    manifest = cache.manifest()
    assert manifest is not None
    assert cache.read_windows(manifest, ("600001",), sessions=sessions)[0] == cache.read_code("600001")


def test_changed_code_does_not_rewrite_other_range(tmp_path) -> None:
    cache = SQLiteQfqWindowCache(tmp_path, "v2")
    cache.replace_window(_window("600001"), "seed")
    cache.replace_window(_window("300001"), "seed")
    before = _fingerprints(cache.root)
    changed, count = cache.replace_window(_window("600001", price=11.0), "revision")
    assert changed == ("v2/qfq_sse_main.sqlite3",)
    assert count == 251
    assert _fingerprints(cache.root)["qfq_szse_chinext.sqlite3"] == before["qfq_szse_chinext.sqlite3"]


def test_board_routing_covers_all_profiles_without_sidecar(tmp_path) -> None:
    cache = SQLiteQfqWindowCache(tmp_path, "v2")
    codes = ("600001", "605001", "000001", "003001", "300001", "301001", "688001", "689001")
    for code in codes:
        cache.replace_window(_window(code), "seed")
    assert len(tuple(cache.root.glob("*.sqlite3"))) == 4
    assert not (cache.root / "index.json").exists()
    assert cache.codes() == frozenset(codes)
    assert cache.read_code("600002").cells == ()
    manifest = cache.manifest()
    assert len(cache.read_windows(manifest, codes, sessions=20)) == 8
    assert all(len(window.cells) == 20 for window in cache.iter_windows(manifest, sessions=20))
    assert all(len(cache.read_code(code).cells) == 251 for code in codes)


def test_large_local_shard_keeps_primary_key_queries_without_splitting(tmp_path) -> None:
    cache = SQLiteQfqWindowCache(tmp_path, "v2")
    cache.replace_window(_window(), "seed")
    shard = cache.root / "qfq_sse_main.sqlite3"
    with sqlite3.connect(shard) as connection:
        connection.execute("CREATE TABLE local_capacity_probe(payload BLOB)")
        connection.execute("INSERT INTO local_capacity_probe VALUES (zeroblob(11000000))")
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT day,payload FROM bars WHERE code=? ORDER BY day", ("600001",)
        ).fetchall()
    assert shard.stat().st_size > 10_000_000
    assert "PRIMARY KEY" in str(plan) and "TEMP B-TREE" not in str(plan)
    assert len(cache.read_code("600001").cells) == 251
    cache.replace_window(_window("600002"), "seed")
    assert len(tuple(cache.root.glob("*.sqlite3"))) == 1


def test_legacy_layout_fails_explicitly_without_hidden_reads(tmp_path) -> None:
    from trader.infra.atomic_files.json import atomic_write_json

    root = tmp_path / "v2"
    root.mkdir()
    atomic_write_json(root / "index.json", {"600001": "09375.sqlite3"})
    cache = SQLiteQfqWindowCache(tmp_path, "v2")
    with pytest.raises(RuntimeError, match="requires_migration"):
        cache.read_code("600001")
    with pytest.raises(RuntimeError, match="requires_migration"):
        cache.replace_window(_window(), "seed")


def test_tampered_cells_fail_closed(tmp_path) -> None:
    cache = SQLiteQfqWindowCache(tmp_path, "v2")
    cache.replace_window(_window(), "seed")
    shard = next(cache.root.glob("*.sqlite3"))
    with sqlite3.connect(shard) as connection:
        connection.execute("DELETE FROM bars WHERE day=?", (DAYS[-1].isoformat(),))
    with pytest.raises(RuntimeError, match="identity mismatch"):
        cache.read_code("600001")


def test_checkpoint_restart_preserves_all_codes_and_failed_write_does_not_advance(tmp_path, monkeypatch) -> None:
    path = tmp_path / ".checkpoint.json"
    checkpoint = QfqCheckpoint(path)
    for code in ("600001", "300001", "600002"):
        checkpoint.confirm(DAYS[-1], code, "fixture")
    checkpoint = QfqCheckpoint(path)
    assert all(checkpoint.completed(DAYS[-1], code, "fixture") for code in ("600001", "300001", "600002"))

    def fail(*_args):
        raise OSError("write failed")

    monkeypatch.setattr("trader.download.infra.qfq_checkpoint.atomic_write_json", fail)
    with pytest.raises(OSError):
        checkpoint.confirm(DAYS[-1], "600003", "fixture")
    assert not checkpoint.completed(DAYS[-1], "600003", "fixture")
    assert not checkpoint.completed(DAYS[-1] + timedelta(days=1), "600001", "fixture")
    assert not checkpoint.completed(DAYS[-1], "600001", "changed-source")


@dataclass
class Supplier:
    days: tuple[date, ...] = DAYS[1:]
    codes: tuple[str, ...] = ("600001",)
    conflict: bool = False
    fail: bool = False
    calls: list[tuple[str, tuple[date, ...]]] = field(default_factory=list)

    def load_qfq_context(self, as_of: date, sessions: int) -> HistorySupplierContext:
        assert sessions == 251
        return HistorySupplierContext(
            BaoStockCalendar(self.days),
            tuple(BaoStockSecurity(code, "fixture", "main", DAYS[0], None, "fixture") for code in self.codes),
            BaoStockSourceVersions("fixture", "3.11", ()),
        )

    def fetch_window(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> PublishedHistoryWindow:
        self.calls.append((security.code, dates))
        if self.fail:
            raise RuntimeError("supplier fixture failure")
        cells = tuple(_cell(security.code, day, price=11.0 if self.conflict else 10.0) for day in dates)
        return PublishedHistoryWindow(security.code, tuple(project_history_cell(cell) for cell in cells))


def _updater(tmp_path: Path, supplier: Supplier) -> UpdateQfqWindows:
    return UpdateQfqWindows(
        SQLiteQfqWindowCache(tmp_path, "v2"),
        SQLiteQfqWindowCache(tmp_path, "v3"),
        supplier,
        QfqCheckpoint(tmp_path / ".checkpoint.json"),
        lambda: False,
        lambda _message: None,
    )


def test_one_serial_fetch_fills_both_profiles_and_restart_skips_completed(tmp_path) -> None:
    supplier = Supplier(codes=("600001", "300001"))
    updater = _updater(tmp_path, supplier)
    observed = datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
    result = updater.execute(observed)
    assert result.completed_codes == 2 and result.pending_codes == 0
    assert len(supplier.calls) == 2
    assert len(updater.v2.read_code("600001").cells) == 251
    assert len(updater.v3.read_code("600001").cells) == 61
    before = (_fingerprints(tmp_path / "v2"), _fingerprints(tmp_path / "v3"))
    result = _updater(tmp_path, supplier).execute(observed)
    assert result.skipped_codes == 2 and result.changed_files == ()
    assert len(supplier.calls) == 2
    assert (_fingerprints(tmp_path / "v2"), _fingerprints(tmp_path / "v3")) == before


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
    supplier = TencentQfqSupplier(TencentQfqDependencies(Http, lambda: (security,), lambda: False))
    logs = []
    updater = UpdateQfqWindows(
        SQLiteQfqWindowCache(tmp_path, "v2"),
        SQLiteQfqWindowCache(tmp_path, "v3"),
        supplier,
        QfqCheckpoint(tmp_path / ".checkpoint.json"),
        lambda: False,
        logs.append,
    )
    observed = datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
    result = updater.execute(observed)
    assert result.completed_codes == 1 and result.pending_codes == 0
    assert len(updater.v2.read_code(security.code).cells) == sessions
    assert len(updater.v3.read_code(security.code).cells) == min(sessions, 61)
    assert any("合格 1 | 待补 0" in message for message in logs)
    assert len(calls) == 3
    resumed = updater.execute(observed)
    assert resumed.skipped_codes == 1 and resumed.pending_codes == 0
    assert resumed.changed_files == () and resumed.changed_rows == 0
    assert len(calls) == 4  # Calendar refresh only; completed stock is not downloaded again.


def test_incomplete_supplier_gap_reports_dates_without_qualifying_window(tmp_path) -> None:
    supplier = Supplier()
    original = supplier.fetch_window

    def incomplete(security, dates):
        window = original(security, dates)
        missing = dates[-2:]
        return PublishedHistoryWindow(
            security.code,
            tuple(
                replace(cell, unadjusted=None, qfq=None, status="unknown_missing")
                if cell.trade_date in missing
                else cell
                for cell in window.cells
            ),
        )

    supplier.fetch_window = incomplete
    logs = []
    updater = UpdateQfqWindows(
        SQLiteQfqWindowCache(tmp_path, "v2"),
        SQLiteQfqWindowCache(tmp_path, "v3"),
        supplier,
        QfqCheckpoint(tmp_path / ".checkpoint.json"),
        lambda: False,
        logs.append,
    )
    observed = datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
    result = updater.execute(observed)
    assert result.completed_codes == 0 and result.pending_codes == 1
    assert any("供应商仍未返回" in message and DAYS[-1].isoformat() in message for message in logs)
    assert not (tmp_path / "v2").exists() and not (tmp_path / "v3").exists()


def test_all_tencent_waves_finish_before_serial_recovery_and_recovered_checkpoint_is_reused(tmp_path) -> None:
    import threading

    from trader.infra.workers import BoundedExecutor

    events = []
    barrier = threading.Barrier(2)
    main_thread = threading.get_ident()

    class Tencent(Supplier):
        def fetch_window(self, security, dates):
            barrier.wait(timeout=3)
            events.append(("tencent", security.code))
            window = super().fetch_window(security, dates)
            if security.code == "600004":
                raise RuntimeError("supplier_failed")
            if security.code == "600001":
                return replace(
                    window,
                    cells=(
                        replace(window.cells[0], status="unknown_missing", unadjusted=None, qfq=None),
                        *window.cells[1:],
                    ),
                )
            return window

    class Recovery:
        source_identity = "baostock:fixture"

        def fetch_window(self, security, dates):
            assert threading.get_ident() == main_thread
            assert len([event for event in events if event[0] == "tencent"]) == 4
            # Qualified Tencent windows were published before the recovery barrier.
            assert updater.v2.read_code("600002").cells
            assert updater.v2.read_code("600003").cells
            events.append(("baostock", security.code))
            assert len(dates) == 251
            return _window(security.code, dates, price=20)

    logs = []
    supplier = Tencent(codes=("600001", "600002", "600003", "600004"))
    pool = BoundedExecutor(worker_count=2, queue_capacity=0, thread_name_prefix="qfq-recovery-test")
    pool.start()
    try:
        updater = replace(
            _updater(tmp_path, supplier), workers=2, worker_pool=pool, report=logs.append, gap_supplier=Recovery()
        )
        observed = datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
        result = updater.execute(observed)
        assert result.completed_codes == 4 and result.pending_codes == 0
        assert [event[0] for event in events] == ["tencent"] * 4 + ["baostock"] * 2
        assert any("待补股票 2 | 已知缺失股票交易日 1 | 缺日数未确定 1 只" in line for line in logs)
        assert any("已处理 4/4（100.00%）| 合格 4 | 待补 0" in line for line in logs)
        for cache in (updater.v2, updater.v3):
            assert cache.source_identity("600001") == "baostock:fixture"
            assert all(cell.qfq.close_price == 20 for cell in cache.read_code("600001").cells)
        before = (_fingerprints(tmp_path / "v2"), _fingerprints(tmp_path / "v3"))
        resumed = updater.execute(observed)
        assert resumed.skipped_codes == 4 and resumed.changed_files == ()
        assert len(events) == 6
        assert (_fingerprints(tmp_path / "v2"), _fingerprints(tmp_path / "v3")) == before
    finally:
        assert pool.stop(wait=True, cancel_futures=True).completed


@pytest.mark.parametrize("invalid", ("failure", "incomplete", "wrong_dates"))
def test_recovery_failure_preserves_old_stock_and_continues_other_stocks(tmp_path, invalid) -> None:
    calls = []

    class Recovery:
        source_identity = "baostock:fixture"

        def fetch_window(self, security, dates):
            calls.append(security.code)
            if security.code == "600001":
                if invalid == "failure":
                    raise RuntimeError("supplier_failed")
                if invalid == "wrong_dates":
                    return _window(security.code, dates[:-1])
                window = _window(security.code, dates)
                return replace(
                    window,
                    cells=(
                        replace(window.cells[0], status="unknown_missing", unadjusted=None, qfq=None),
                        *window.cells[1:],
                    ),
                )
            return _window(security.code, dates)

    updater = replace(_updater(tmp_path, Supplier(codes=("600001", "600002"), fail=True)), gap_supplier=Recovery())
    updater.seed((_window("600001", price=15),), "old")
    previous = updater.v2.read_code("600001")
    observed = datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
    result = updater.execute(observed)
    assert calls == ["600001", "600002"]
    assert result.completed_codes == 1 and result.pending_codes == 1
    assert updater.v2.read_code("600001") == previous
    assert updater.v2.source_identity("600001") == "old"
    source = updater.v2.source_identity("600002")
    assert source == "baostock:fixture"
    assert not updater.resume.completed(DAYS[-1], "600001", source)


@pytest.mark.parametrize("phase", ("tencent", "baostock"))
def test_cancel_does_not_start_recovery_or_publish_late_recovery_result(tmp_path, phase) -> None:
    cancelled = False
    calls = []

    class Tencent(Supplier):
        def fetch_window(self, security, dates):
            nonlocal cancelled
            if phase == "tencent":
                cancelled = True
            raise RuntimeError("supplier_failed")

    class Recovery:
        source_identity = "baostock:fixture"

        def fetch_window(self, security, dates):
            nonlocal cancelled
            calls.append(security.code)
            cancelled = True
            return _window(security.code, dates)

    updater = replace(
        _updater(tmp_path, Tencent(codes=("600001", "600002"))),
        cancel_requested=lambda: cancelled,
        gap_supplier=Recovery(),
    )
    observed = datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
    result = updater.execute(observed)
    assert result.failure_reason == "cancelled" and result.pending_codes == 2
    assert result.completed_codes == 0 and result.changed_files == ()
    assert len(calls) == (1 if phase == "baostock" else 0)
    assert not (tmp_path / ".checkpoint.json").exists()


def test_new_day_refetches_tencent_full_window_after_baostock_recovery(tmp_path) -> None:
    class Recovery:
        source_identity = "baostock:fixture"

        def fetch_window(self, security, dates):
            return _window(security.code, dates, price=20)

    supplier = Supplier(days=DAYS[:-1], fail=True)
    updater = replace(_updater(tmp_path, supplier), gap_supplier=Recovery())
    observed = datetime.combine(DAYS[-2], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
    assert updater.execute(observed).completed_codes == 1
    supplier.fail = False
    supplier.days = DAYS[1:]
    supplier.calls.clear()
    result = updater.execute(observed + timedelta(days=1))
    assert result.completed_codes == 1 and result.pending_codes == 0
    assert supplier.calls == [("600001", DAYS[1:])]
    assert updater.v2.source_identity("600001").startswith("fixture:")
    assert all(cell.qfq.close_price == 10 for cell in updater.v2.read_code("600001").cells)


def test_complete_tencent_stocks_never_call_recovery(tmp_path) -> None:
    class Recovery:
        source_identity = "baostock:fixture"

        def fetch_window(self, security, dates):
            raise AssertionError("BaoStock should not be called")

    updater = replace(_updater(tmp_path, Supplier()), gap_supplier=Recovery())
    observed = datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
    assert updater.execute(observed).completed_codes == 1


def test_qfq_composition_wires_lazy_baostock_recovery_and_closes_resources(tmp_path, monkeypatch) -> None:
    from trader import bootstrap
    from trader.download.domain.baostock_daily import BaoStockCodeBatch, BaoStockCodeDownload, BaoStockDailyFact

    events = []

    class Tencent(Supplier):
        def fetch_window(self, security, dates):
            events.append("tencent")
            raise RuntimeError("supplier_failed")

    class BaoStock:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            events.append("closed")

        def fetch_code(self, security, dates):
            assert events.count("tencent") == 2
            events.append("baostock")
            batch = BaoStockCodeBatch(security.code, tuple(_cell(security.code, day) for day in dates))
            return BaoStockCodeDownload(batch, tuple(BaoStockDailyFact(security.code, day, False) for day in dates))

    monkeypatch.setattr(bootstrap, "TencentQfqSupplier", lambda _dependencies: Tencent(codes=("600001", "600002")))
    monkeypatch.setattr(bootstrap, "BaoStockHistorySupplier", lambda *_args, **_kwargs: BaoStock())
    result = bootstrap.execute_qfq_download(
        tmp_path,
        report=lambda _message: None,
        now=lambda: datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16),
    )
    assert result.completed_codes == 2 and result.pending_codes == 0
    assert events == ["tencent", "tencent", "baostock", "baostock", "closed"]
    assert SQLiteQfqWindowCache(tmp_path / "data/qfq", "v2").source_identity("600001").startswith("baostock:")


@pytest.mark.parametrize("conflict", (False, True))
def test_small_tail_exact_overlap_or_bounded_revision_refetch(tmp_path, conflict) -> None:
    supplier = Supplier(conflict=conflict)
    updater = _updater(tmp_path, supplier)
    context = supplier.load_qfq_context(DAYS[-1], 251)
    source = f"fixture:{context.source_versions.content_hash}"
    updater.seed((_window(days=DAYS[:-1]),), source)
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
    updater.seed((_window(),), source)
    result = updater.execute(datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16))
    assert result.changed_files == ()
    assert supplier.calls == [("600001", DAYS[-3:])]


def test_interrupted_between_profiles_does_not_advance_checkpoint_and_retry_repairs(tmp_path, monkeypatch) -> None:
    supplier = Supplier()
    updater = _updater(tmp_path, supplier)
    write = updater.v3.replace_window

    def fail(*_args):
        raise RuntimeError("interrupted second profile")

    monkeypatch.setattr(updater.v3, "replace_window", fail)
    observed = datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
    result = updater.execute(observed)
    assert result.pending_codes == 1
    source = f"fixture:{supplier.load_qfq_context(DAYS[-1], 251).source_versions.content_hash}"
    assert not updater.resume.completed(DAYS[-1], "600001", source)
    assert updater.v2.read_code("600001").cells
    monkeypatch.setattr(updater.v3, "replace_window", write)
    result = updater.execute(observed)
    assert result.pending_codes == 0 and result.completed_codes == 1
    assert len(updater.v3.read_code("600001").cells) == 61
    assert updater.resume.completed(DAYS[-1], "600001", source)


def test_source_migration_ignores_legacy_checkpoint_and_refetches_full_window(tmp_path):
    supplier = Supplier()
    updater = _updater(tmp_path, supplier)
    updater.seed((_window(),), "history:seed")
    source = f"fixture:{supplier.load_qfq_context(DAYS[-1], 251).source_versions.content_hash}"
    # Even a valid same-day code checkpoint cannot relabel old-source rows.
    updater.resume.confirm(DAYS[-1], "600001", source)
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
    original = updater.v2.replace_window

    def write(window, identity):
        assert threading.get_ident() == writer_ident
        return original(window, identity)

    monkeypatch.setattr(updater.v2, "replace_window", write)
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
    original = updater.v2.read_code

    def read(code):
        if code == "600001":
            raise RuntimeError("corrupt shard")
        return original(code)

    monkeypatch.setattr(updater.v2, "read_code", read)
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
            return super().fetch_window(security, dates)

    supplier = OutOfOrderSupplier(codes=("600001", "600002", "600003"))
    updater = _updater(tmp_path, supplier)
    original = updater.v3.replace_window

    def write(window, identity):
        result = original(window, identity)
        writes.append(window.code)
        if window.code == "600002":
            second_published.set()
        return result

    def report(message):
        assert threading.get_ident() == writer_ident
        messages.append(message)

    monkeypatch.setattr(updater.v3, "replace_window", write)
    pool = BoundedExecutor(worker_count=2, queue_capacity=0, thread_name_prefix="qfq-progress-fixture")
    pool.start()
    try:
        updater = replace(updater, workers=2, worker_pool=pool, report=report)
        observed = datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
        result = updater.execute(observed)
        assert result.completed_codes == 3
        assert writes == ["600002", "600001", "600003"]
        batches = [message for message in messages if " | qfq 批次完成 | " in message]
        assert len(batches) == 2
        assert "已处理 2/3（66.67%）| 合格 2 | 待补 0" in batches[0]
        assert "已处理 3/3（100.00%）" in batches[1]
        starts = [message for message in messages if " | qfq 下载 | " in message]
        assert "本批 2 只 | 股票 600001 fixture、600002 fixture" in starts[0]
        assert "本批 1 只 | 股票 600003 fixture" in starts[1]
        assert " | qfq 完成 | " in messages[-1]
        assert "股票 3 只" in messages[1] and "并发 2" in messages[1]

        messages.clear()
        resumed = updater.execute(observed)
        assert resumed.skipped_codes == 3 and resumed.changed_files == ()
        assert len(supplier.calls) == 3
        assert "合格 3 | 待补 0 | 其中复用 3" in messages[-1]
        assert "未处理 0" in messages[-1]
    finally:
        assert pool.stop(wait=True, cancel_futures=True).completed


class _IncompleteSupplier(Supplier):
    def fetch_window(self, security, dates):
        window = super().fetch_window(security, dates)
        if security.code == "600001":
            return replace(
                window,
                cells=tuple(
                    replace(cell, status="unknown_missing", unadjusted=None, qfq=None) if index < 4 else cell
                    for index, cell in enumerate(window.cells)
                ),
            )
        return window


def test_pending_progress_groups_named_chinese_reasons_after_batch_and_preserves_old_window(tmp_path):
    supplier = _IncompleteSupplier(codes=("600001", "600002"))
    messages = []
    updater = replace(_updater(tmp_path, supplier), report=messages.append)
    updater.seed((_window(),), "old-source")
    before = updater.v2.read_code("600001")
    observed = datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
    result = updater.execute(observed)
    assert result.completed_codes == 1 and result.pending_codes == 1
    assert updater.v2.read_code("600001") == before
    summary = next(index for index, message in enumerate(messages) if " | qfq 批次完成 | " in message)
    assert "已处理 2/2（100.00%）| 合格 1 | 待补 1" in messages[summary]
    assert "qfq 待补 | 600001 fixture，未复权缺 4 日、前复权缺 4 日" in messages[summary + 1]
    assert messages[summary + 1].endswith("reason=qfq_incomplete_raw_4_qfq_4")
    assert " | qfq 结束（仍有待补） | " in messages[-1]


def test_tencent_diagnostic_counts_pending_with_new_batch_feedback():
    from scripts.runtime_diagnostics.tencent_download import _benchmark

    supplier = _IncompleteSupplier(codes=("600001", "600002"))
    context = supplier.load_qfq_context(DAYS[-1], 251)
    observed = datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
    report = _benchmark(supplier, context, 2, 2, observed)
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
    result = replace(updater, report=messages.append).execute(observed)
    starts = [message for message in messages if " | qfq 下载 | " in message]
    assert len(starts) == 2
    assert "本批 2 只 | 股票 600002 fixture、600004 fixture" in starts[0]
    assert "本批 2 只 | 股票 600006 fixture、600008 fixture" in starts[1]
    assert result.completed_codes == 8 and result.skipped_codes == 4
    assert len(supplier.calls) == 8


def test_preparation_pending_is_reported_without_any_download_and_hides_payload(tmp_path, monkeypatch):
    supplier = Supplier()
    messages = []
    updater = replace(_updater(tmp_path, supplier), report=messages.append)

    def read(_code):
        raise RuntimeError("private supplier payload\nhttps://example.invalid?token=secret")

    monkeypatch.setattr(updater.v2, "read_code", read)
    observed = datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
    result = updater.execute(observed)
    assert result.completed_codes == 0 and result.pending_codes == 1
    assert supplier.calls == []
    assert "已处理 1/1（100.00%）| 合格 0 | 待补 1" in messages[-1]
    details = next(message for message in messages if " | qfq 待补 | " in message)
    assert "600001 fixture，处理失败（RuntimeError）" in details
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
    updater = replace(_updater(tmp_path, supplier), report=messages.append, monotonic=lambda: clock[0])
    observed = datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
    result = updater.execute(observed)
    assert result.completed_codes == result.pending_codes == result.skipped_codes == 0
    assert supplier.calls == []
    assert messages[0].startswith("00:00:00 | qfq 准备 | ")
    assert messages[-1].startswith("00:01:05 | qfq 完成 | 已处理 1/1（100.00%）")
    assert "合格 0 | 待补 0 | 其中复用 0 | 无需下载 1 | 未处理 0" in messages[-1]


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
        _updater(tmp_path, supplier), report=messages.append, workers=2, cancel_requested=lambda: cancelled
    )
    observed = datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
    result = updater.execute(observed)
    assert result.failure_reason == "cancelled" and result.pending_codes == 3
    assert result.completed_codes == 0 and result.changed_files == ()
    assert len(supplier.calls) == 1
    assert " | qfq 已取消 | 已处理 0/3（0.00%）| 合格 0 | 待补 0" in messages[-1]
    assert "未处理 3" in messages[-1]
    assert not any(" | qfq 待补 | " in message for message in messages)


def test_context_failure_reports_bounded_reason_and_preparation_elapsed(tmp_path):
    clock = [100.0]

    class BrokenContextSupplier(Supplier):
        def load_qfq_context(self, as_of, sessions):
            clock[0] += 3
            raise RuntimeError("private supplier payload")

    messages = []
    updater = replace(_updater(tmp_path, BrokenContextSupplier()), report=messages.append, monotonic=lambda: clock[0])
    with pytest.raises(RuntimeError, match="private supplier payload"):
        updater.execute(datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16))
    assert messages[-1] == "00:00:03 | qfq 准备失败 | RuntimeError"


@pytest.mark.parametrize(
    "hour,minute,due", ((8, 0, False), (12, 0, False), (15, 9, False), (15, 10, True), (21, 0, True))
)
def test_hot_and_late_cold_start_daily_schedule(hour, minute, due) -> None:
    now = datetime(2026, 10, 9, hour, minute, tzinfo=SHANGHAI)
    calls = []

    def update(cancel):
        assert not cancel()
        calls.append(now)
        return QfqUpdateResult(now.date(), 1)

    maintenance = QfqDailyMaintenance(update, now=lambda: now)
    assert maintenance.tick() is due
    assert len(calls) == int(due)
    assert not maintenance.tick()
    assert completed_daily_cutoff(now) == now.date() - timedelta(days=not due)


def test_retry_delay_completion_next_day_and_cold_missing_database() -> None:
    now = datetime(2026, 10, 9, 15, 10, tzinfo=SHANGHAI)
    elapsed = 0.0
    calls = 0

    def update(_cancel):
        nonlocal calls
        calls += 1
        return QfqUpdateResult(now.date(), pending_codes=int(calls == 1))

    maintenance = QfqDailyMaintenance(update, now=lambda: now, monotonic=lambda: elapsed)
    assert maintenance.tick()
    elapsed = 1799
    assert not maintenance.tick()
    elapsed = 1800
    assert maintenance.tick()
    elapsed = 4000
    assert not maintenance.tick()
    now += timedelta(days=1)
    assert maintenance.tick()
    now = now.replace(hour=8)
    cold = QfqDailyMaintenance(update, now=lambda: now, needs_initialization=lambda: True)
    assert cold.tick()
    result = cold.stop(wait=True)
    assert result.completed


def test_online_projection_matches_full_history_and_keeps_settlement_independent(tmp_path) -> None:
    from tests.component.test_candidate_history_tail import CALENDAR, NOW, _Archive

    full = _Archive(251, 0, ("600001", "600002"))
    reader = ReadPublishedHistoryUseCase(full)
    short = SQLiteQfqWindowCache(tmp_path, "v2")
    for window in reader.iter_windows(full.current, sessions=251):
        if window.code == "600002":
            window = PublishedHistoryWindow(window.code, window.cells[:-2])
        short.replace_window(window, "history:fixture")
    calendar_calls = 0

    def dates():
        nonlocal calendar_calls
        calendar_calls += 1
        return CALENDAR

    projection = PublishedHistoryCache(
        ReadPublishedHistoryUseCase(short),
        lookback_sessions=251,
        outcome_history=reader,
        open_dates=dates,
    )
    original = PublishedHistoryCache(reader, lookback_sessions=251)
    assert projection.refresh() and original.refresh()
    restrictions = {}
    loaded = projection.load(("600001", "600002"), observed_at=NOW, action_restrictions=restrictions)
    assert tuple(loaded) == ("600001",)
    assert calendar_calls == 1
    assert restrictions == {"600002": {"history_data_pending"}}
    expected = original.load(("600001",), observed_at=NOW)
    assert loaded == expected
    assert projection.summaries(loaded, NOW) == original.summaries(expected, NOW)
    missing = PublishedHistoryCache(
        ReadPublishedHistoryUseCase(SQLiteQfqWindowCache(tmp_path / "missing", "v2")),
        lookback_sessions=251,
        outcome_history=reader,
    )
    assert missing.read_outcome_bars(("600001",), NOW) == original.read_outcome_bars(("600001",), NOW)
    assert missing.load(("600001",), observed_at=NOW) == {}


def test_runner_lock_blocks_network_and_local_seeding_is_resumable(tmp_path) -> None:
    from tests.component.test_candidate_history_tail import NOW, _Archive

    history = _Archive(251, 0, ("600001", "600002"))
    supplier = Supplier()
    updater = _updater(tmp_path, supplier)
    runner = QfqUpdateRunner(
        ReadPublishedHistoryUseCase(history),
        updater,
        updater.v2,
        updater.v3,
        tmp_path / ".lock",
        lambda: NOW,
    )
    with HistoryMaintenanceLock(tmp_path / ".lock"):
        result = runner.execute()
        assert result.failure_reason is not None
    assert supplier.calls == []
    result = runner.execute(seed_only=True)
    assert result.completed_codes == 2 and result.changed_rows == 624
    before = (_fingerprints(tmp_path / "v2"), _fingerprints(tmp_path / "v3"))
    assert runner.execute(seed_only=True).changed_files == ()
    assert (_fingerprints(tmp_path / "v2"), _fingerprints(tmp_path / "v3")) == before
    assert supplier.calls == []


def test_tencent_runner_never_reads_full_history_for_online_update(tmp_path):
    class UnavailableHistory:
        def manifest(self):
            raise AssertionError("online qfq must not inspect full history")

    supplier = Supplier(codes=("600001",))
    updater = _updater(tmp_path, supplier)
    observed = datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
    runner = QfqUpdateRunner(
        UnavailableHistory(), updater, updater.v2, updater.v3, tmp_path / ".lock", lambda: observed
    )
    result = runner.execute()
    assert result.completed_codes == 1
    assert len(updater.v2.read_code("600001").cells) == 251
    assert len(updater.v3.read_code("600001").cells) == 61


@pytest.mark.parametrize("pending,exit_code", ((0, 0), (2, 1)))
def test_qfq_cli_is_zero_argument_and_reports_pending(tmp_path, monkeypatch, capsys, pending, exit_code) -> None:
    calls = []

    def execute(root, *, report):
        calls.append(root)
        report("qfq fixture progress")
        return QfqUpdateResult(DAYS[-1], pending_codes=pending)

    monkeypatch.setattr(cli, "load_runtime_settings", lambda _path: SimpleNamespace(project_root=tmp_path))
    monkeypatch.setattr("trader.bootstrap.execute_qfq_download", execute)
    assert cli.main(["--config", str(tmp_path / "runtime.json"), "qfq_download"]) == exit_code
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == "qfq fixture progress\n"
    assert payload["pending_codes"] == pending
    assert calls == [tmp_path]
    with pytest.raises(SystemExit) as rejected:
        cli.main(["--config", str(tmp_path / "runtime.json"), "qfq_download", "--profile", "v3"])
    assert rejected.value.code == 2
    assert calls == [tmp_path]


def test_local_extraction_heartbeat_is_visible_and_thread_stops() -> None:
    import threading

    from trader.download.infra.qfq_progress import qfq_local_progress

    feedback = threading.Event()
    messages = []

    def report(message):
        messages.append(message)
        if "waiting elapsed=" in message:
            feedback.set()

    with qfq_local_progress(report, interval_seconds=0.01):
        assert messages[0] == "qfq history extraction: started"
        assert feedback.wait(2)
    assert "finished elapsed=" in messages[-1]
    assert not any(thread.name == "qfq-extraction-progress" for thread in threading.enumerate())

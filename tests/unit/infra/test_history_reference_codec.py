from __future__ import annotations

import json
from contextlib import closing, nullcontext
from dataclasses import replace
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from tests.unit.infra.research.test_history_sync import NOW, FakeSupplier
from tests.unit.infra.test_history_supplier_router import Baseline, Prices, _current_universe
from trader.download.domain.baostock_daily import BaoStockIndustryInterval
from trader.download.domain.history_reference import HistoryReferenceSnapshot, HistoryStEvidence
from trader.download.domain.history_sync import HistoryStEligibilitySummary, HistorySyncConfiguration
from trader.download.domain.published_history import PublishedHistoryCell, PublishedHistoryWindow
from trader.download.domain.security_eligibility import IsStSecurityName
from trader.download.infra.history_control_index import HistoryControlIndex
from trader.download.infra.history_reference_codec import (
    HistoryReferenceIndex,
    HistoryStEvidencePath,
    ReadHistoryReference,
)
from trader.download.infra.history_snapshot_reader import HistorySnapshotReader
from trader.download.infra.history_st_source import HistoryStNameSource, ParseNameHistorySt
from trader.download.infra.history_supplier_router import HistorySupplierRouter
from trader.download.infra.history_sync_runner import RunHistorySync
from trader.infra.sqlite_connection import OpenSqliteConnection


@pytest.mark.parametrize(
    ("names", "expected"),
    (
        ("ST自仪(ST自仪Ｂ) SST自仪 自仪股份 上海临港", "ever_st"),
        ("*ST金科 金科股份", "ever_st"),
        ("ＳＴ公司 正常", "ever_st"),
        ("平安银行 深发展A", "clear"),
    ),
)
def test_name_history_keeps_delisted_st_names(names, expected):
    html = f'<td>证券简称更名历史：</td><td colspan="3">{names}</td>'
    assert ParseNameHistorySt("600848", "上海临港", date(2026, 10, 9), html).status == expected


@pytest.mark.parametrize(
    "text",
    ("maintenance", "<td>证券简称更名历史：</td><td>--</td>", "<td>证券简称更名历史：</td><td>－－</td>"),
)
def test_missing_name_history_never_means_clear(text):
    with pytest.raises(ValueError):
        ParseNameHistorySt("600848", "上海临港", date(2026, 10, 9), text)


@pytest.mark.parametrize((("current_name", "expected")), (("青岛特锐", "clear"), ("*ST测试", "ever_st")))
def test_explicit_empty_name_history_uses_the_official_current_name(current_name, expected):
    html = '<td>证券简称更名历史：</td><td colspan="3"></td>'

    assert ParseNameHistorySt("300001", current_name, date(2026, 10, 10), html).status == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    (("ST豆神", True), ("*ST仕净", True), ("ＳＴ公司", True), ("S*ST公司", True), ("正常公司", False)),
)
def test_st_name_rule_normalizes_current_and_historical_evidence(name, expected):
    assert IsStSecurityName(name) is expected


def test_st_filter_removes_all_history_for_ever_st_and_unknown_before_baseline(tmp_path):
    dates = (date(2026, 9, 8), date(2026, 9, 9))

    class MixedSt:
        def fetch(self, universe, as_of):
            return tuple(
                HistoryStEvidence(item.code, as_of, status)
                for item, status in zip(universe, ("clear", "ever_st", "unknown"), strict=True)
            )

    class RecordingBaseline(Baseline):
        def load_context(self, as_of, sessions, *, universe, industry_cache=None):
            assert tuple(item.code for item in universe) == ("600001",)
            return super().load_context(as_of, sessions, universe=universe, industry_cache=industry_cache)

    config = HistorySyncConfiguration(
        tmp_path,
        sessions=2,
        reread_sessions=2,
        minimum_free_bytes=0,
        reference_root=(tmp_path).parent / "reference",
        training_root=(tmp_path).parent / "models",
    )
    baseline, prices = RecordingBaseline(dates), Prices()
    result = RunHistorySync(
        config,
        HistorySupplierRouter(baseline, prices, lambda: _current_universe("600001", "600002", "600003"), MixedSt()),
        clock=lambda: NOW,
    )
    assert result.state == "completed"
    assert baseline.calls == []
    published = HistoryControlIndex(tmp_path / "catalog.sqlite3").load_published_state()
    assert tuple(item.code for item in published.universe.securities) == ("600001",)


def test_unknown_tencent_date_breaks_training_window_and_is_refetched(tmp_path):
    dates = tuple(date(2026, 7, 1) + timedelta(days=i) for i in range(125))
    gap = dates[61]

    class Sparse(FakeSupplier):
        def fetch_tencent_window(self, security, requested):
            window = super().fetch_tencent_window(security, requested)
            if security.code != "600001":
                return window
            return replace(
                window,
                cells=tuple(
                    PublishedHistoryCell(security.code, day, "unknown_missing", None, None) if day == gap else cell
                    for day, cell in zip(requested, window.cells, strict=True)
                ),
            )

    config = HistorySyncConfiguration(
        tmp_path,
        sessions=125,
        minimum_free_bytes=0,
        reference_root=(tmp_path).parent / "reference",
        training_root=(tmp_path).parent / "models",
    )
    result = RunHistorySync(config, Sparse(dates, industry="bank"), clock=lambda: NOW)
    assert result.state == "completed" and result.unresolved_price_cells == 1
    control = HistoryControlIndex(tmp_path / "catalog.sqlite3")
    snapshot = control.load_state().active_snapshot
    reader = HistorySnapshotReader(tmp_path)
    rows = tuple(reader.iter_training_rows(snapshot, dates, {"600001"}))
    assert next(row for row in rows if row.trade_date == gap).point is None
    assert reader.read_day(gap, snapshot)[0].cell.status == "unknown_missing"
    resumed = FakeSupplier(dates, industry="bank")
    result = RunHistorySync(config, resumed, clock=lambda: NOW)
    assert result.state == "completed" and result.unresolved_price_cells == 0
    assert resumed.calls == [("600001", (gap, *dates[-5:]))]
    assert reader.read_day(gap, snapshot)[0].cell.status == "unknown_missing"


def test_tencent_missing_dates_publish_without_baostock_recovery(tmp_path):
    dates = (date(2026, 9, 8), date(2026, 9, 9))

    class Missing(FakeSupplier):
        def fetch_tencent_window(self, security, requested):
            return PublishedHistoryWindow(
                security.code,
                tuple(PublishedHistoryCell(security.code, day, "unknown_missing", None, None) for day in requested),
            )

    config = HistorySyncConfiguration(
        tmp_path,
        sessions=2,
        reread_sessions=2,
        minimum_free_bytes=0,
        reference_root=(tmp_path).parent / "reference",
        training_root=(tmp_path).parent / "models",
    )
    supplier = Missing(dates)
    first = RunHistorySync(config, supplier, clock=lambda: NOW)
    assert first.state == "completed" and first.unresolved_price_cells == 4
    assert "baostock_prices" not in supplier.events
    second = RunHistorySync(config, supplier, clock=lambda: NOW)
    assert second.state == "completed" and second.unresolved_price_cells == 4


def test_reference_files_validate_hash_and_preserve_dated_industries(tmp_path):
    code = "600001"
    reference = HistoryReferenceSnapshot(
        (code,),
        (
            BaoStockIndustryInterval(code, date(2020, 1, 1), date(2023, 1, 1), "bank", "csrc"),
            BaoStockIndustryInterval(code, date(2023, 1, 1), None, "finance", "csrc"),
        ),
        (HistoryStEvidence(code, date(2026, 10, 9), "clear"),),
    )
    path = tmp_path / "catalog.sqlite3"
    control = HistoryControlIndex(path)
    control.initialize()
    control.save_reference(reference)
    assert ReadHistoryReference(path, reference.content_hash) == reference
    with closing(OpenSqliteConnection(path)) as connection:
        payload = json.loads(connection.run("SELECT payload_json FROM bound_references").require_one()[0])
    assert payload["st_statuses"] == {code: ["2026-10-09", "clear"]}
    assert payload["industries"][code] == [
        ["2020-01-01", "2023-01-01", "bank", "csrc"],
        ["2023-01-01", None, "finance", "csrc"],
    ]
    assert "st_evidence" not in payload and "industry_intervals" not in payload
    payload["eligible_codes"] = ["600002"]
    with closing(OpenSqliteConnection(path)) as connection, connection:
        connection.run("UPDATE bound_references SET payload_json=?", (json.dumps(payload),))
    with pytest.raises(ValueError):
        ReadHistoryReference(path, reference.content_hash)


def test_reference_index_requires_a_continuous_industry_window() -> None:
    code = "600001"
    dates = tuple(date(2022, 9, 1) + timedelta(days=offset) for offset in range(300))
    covered = HistoryReferenceSnapshot(
        (code,),
        (
            BaoStockIndustryInterval(code, dates[0], date(2023, 1, 1), "bank", "csrc"),
            BaoStockIndustryInterval(code, date(2023, 1, 1), None, "finance", "csrc"),
        ),
        (HistoryStEvidence(code, dates[-1], "clear"),),
    )
    current_only = replace(
        covered,
        industry_intervals=(BaoStockIndustryInterval(code, dates[-1], None, "finance", "csrc"),),
    )

    assert HistoryReferenceIndex(covered).has_industry_window((code,), dates, 251)
    assert not HistoryReferenceIndex(covered).has_industry_window(
        (code,), dates, 251, anchor_dates=frozenset(dates[:100])
    )
    assert HistoryReferenceIndex(covered).has_industry_window((code,), dates, 251, anchor_dates=frozenset(dates[-1:]))
    assert not HistoryReferenceIndex(current_only).has_industry_window((code,), dates, 251)


def test_generated_st_evidence_uses_the_injected_reference_root(tmp_path):
    reference_root = tmp_path / "data" / "reference"

    assert HistoryStEvidencePath(reference_root) == reference_root / "historical_st.sqlite3"


def test_st_checkpoints_cache_clear_and_permanently_keep_ever_st(tmp_path):
    from trader.infra.workers import InjectedExecutor

    calls = []

    class Response:
        def __init__(self, names):
            self.content = f"<td>证券简称更名历史：</td><td>{names}</td>".encode("gb18030")

        def raise_for_status(self):
            pass

        def close(self):
            pass

    def get(url, **kwargs):
        calls.append(url)
        assert kwargs["timeout"] == 15.0
        return Response("*ST曾用 正常公司" if "600002" in url else "正常公司")

    source = HistoryStNameSource(
        tmp_path / "st.json",
        lambda: nullcontext(SimpleNamespace(get=get)),
        InjectedExecutor(None),
        lambda: False,
        report=lambda *_: None,
    )
    universe = _current_universe("600001", "600002")
    as_of = date(2026, 10, 9)
    assert tuple(item.status for item in source.fetch(universe, as_of)) == ("clear", "ever_st")
    assert source.fetch(universe, as_of)
    assert len(calls) == 2
    assert source.fetch(universe, as_of + timedelta(days=1))
    assert len(calls) == 3 and "600001" in calls[-1]


def test_missing_reference_fails_closed_for_training(tmp_path):
    from trader.download.infra.history_snapshot_reader import HistorySnapshotReadError

    dates = tuple(date(2026, 7, 1) + timedelta(days=i) for i in range(65))
    config = HistorySyncConfiguration(
        tmp_path,
        sessions=65,
        minimum_free_bytes=0,
        reference_root=(tmp_path).parent / "reference",
        training_root=(tmp_path).parent / "models",
    )
    assert RunHistorySync(config, FakeSupplier(dates, industry="bank"), clock=lambda: NOW).state == "completed"
    snapshot = HistoryControlIndex(tmp_path / "catalog.sqlite3").load_published_state().snapshot
    reader = HistorySnapshotReader(tmp_path)
    assert tuple(reader.iter_training_rows(snapshot, dates, {"600001"}))
    source = HistoryControlIndex(tmp_path / "catalog.sqlite3").read_source(snapshot.source_identity_hash)
    with closing(OpenSqliteConnection(tmp_path / "catalog.sqlite3")) as connection, connection:
        connection.run("DELETE FROM bound_references WHERE content_hash=?", (source.supplier_contract[12:76],))
    with pytest.raises(HistorySnapshotReadError, match="history_reference_unavailable"):
        tuple(HistorySnapshotReader(tmp_path).iter_training_rows(snapshot, dates, {"600001"}))


def test_tencent_missing_dates_publish_sparse_rows_for_every_stock(tmp_path):
    dates = (date(2026, 9, 8), date(2026, 9, 9))

    class Missing(FakeSupplier):
        def fetch_tencent_window(self, security, requested):
            return PublishedHistoryWindow(
                security.code,
                tuple(PublishedHistoryCell(security.code, day, "unknown_missing", None, None) for day in requested),
            )

    config = HistorySyncConfiguration(
        tmp_path,
        sessions=2,
        reread_sessions=2,
        minimum_free_bytes=0,
        reference_root=(tmp_path).parent / "reference",
        training_root=(tmp_path).parent / "models",
    )
    supplier = Missing(dates)
    result = RunHistorySync(config, supplier, clock=lambda: NOW)
    assert result.state == "completed" and result.unresolved_price_cells == 4
    assert supplier.calls == []
    snapshot = HistoryControlIndex(tmp_path / "catalog.sqlite3").load_state().active_snapshot
    rows = HistorySnapshotReader(tmp_path).read_day(dates[-1], snapshot)
    assert [(row.code, row.cell.obtained) for row in rows] == [("600001", False), ("600002", False)]


def test_small_worker_pool_checks_every_code_without_rejection(tmp_path):
    import threading

    from trader.infra.workers import BoundedExecutor

    rendezvous = threading.Barrier(2)

    class Response:
        content = "<td>证券简称更名历史：</td><td>正常公司</td>".encode("gb18030")

        def raise_for_status(self):
            pass

        def close(self):
            pass

    def get(*_args, **_kwargs):
        rendezvous.wait(timeout=3)
        return Response()

    executor = BoundedExecutor(worker_count=2, queue_capacity=0, thread_name_prefix="st-test")
    executor.start()
    reports = []
    try:
        source = HistoryStNameSource(
            tmp_path / "st.json",
            lambda: nullcontext(SimpleNamespace(get=get)),
            executor,
            lambda: False,
            report=lambda *counts: reports.append(counts),
            batch_size=2,
        )
        result = source.fetch(_current_universe("600001", "600002", "600003", "600004"), date(2026, 10, 9))
        assert len(result) == 4 and all(item.status == "clear" for item in result)
        assert reports[0] == (0, 4, None)
        assert reports[1] == (2, 4, None)
        assert reports[2][:2] == (4, 4)
        assert reports[2][2] == HistoryStEligibilitySummary(4, 0, 0)
        assert executor.status().rejected_count == 0
    finally:
        executor.stop(wait=True, cancel_futures=True)


def test_st_checks_wait_for_transient_worker_capacity(tmp_path):
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

    class Response:
        content = "<td>证券简称更名历史：</td><td>正常公司</td>".encode("gb18030")

        def raise_for_status(self):
            pass

        def close(self):
            pass

    with ThreadPoolExecutor(max_workers=2) as pool:
        worker = RejectOnce(pool)
        source = HistoryStNameSource(
            tmp_path / "st.json",
            lambda: nullcontext(SimpleNamespace(get=lambda *_args, **_kwargs: Response())),
            worker,
            lambda: False,
            report=lambda *_counts: None,
            batch_size=2,
        )
        result = source.fetch(_current_universe("600001", "600002"), date(2026, 10, 9))
    assert worker.rejections == 1
    assert len(result) == 2 and all(item.status == "clear" for item in result)


def test_st_checks_refill_available_worker_without_waiting_for_slowest_stock(tmp_path):
    import threading

    from trader.infra.workers import BoundedExecutor

    third_started = threading.Event()
    second_observed_refill = False

    class Response:
        content = "<td>证券简称更名历史：</td><td>正常公司</td>".encode("gb18030")

        def raise_for_status(self):
            pass

        def close(self):
            pass

    def get(url, **_kwargs):
        nonlocal second_observed_refill
        if "600002" in url:
            second_observed_refill = third_started.wait(timeout=3)
        elif "600003" in url:
            third_started.set()
        return Response()

    executor = BoundedExecutor(worker_count=2, queue_capacity=0, thread_name_prefix="st-rolling-test")
    executor.start()
    try:
        source = HistoryStNameSource(
            tmp_path / "st.json",
            lambda: nullcontext(SimpleNamespace(get=get)),
            executor,
            lambda: False,
            report=lambda *_counts: None,
            batch_size=2,
        )
        assert len(source.fetch(_current_universe("600001", "600002", "600003"), date(2026, 10, 9))) == 3
    finally:
        executor.stop(wait=True, cancel_futures=True)

    assert second_observed_refill


@pytest.mark.parametrize("damage", ("missing", "tampered"))
def test_sync_restores_bound_reference_without_redownloading_prices(tmp_path, damage):
    from trader.download.infra.history_snapshot_inspection import InspectHistorySnapshot

    root = tmp_path / "baostock"
    dates = (date(2026, 9, 8), date(2026, 9, 9))
    supplier = FakeSupplier(dates, industry="bank")
    config = HistorySyncConfiguration(
        root,
        sessions=2,
        reread_sessions=2,
        minimum_free_bytes=0,
        reference_root=(root).parent / "reference",
        training_root=(root).parent / "models",
    )
    assert RunHistorySync(config, supplier, clock=lambda: NOW).state == "completed"
    snapshot = HistoryControlIndex(root / "catalog.sqlite3").load_published_state().snapshot
    HistorySnapshotReader(root).reference_index(snapshot)
    source = HistoryControlIndex(root / "catalog.sqlite3").read_source(snapshot.source_identity_hash)
    with closing(OpenSqliteConnection(root / "catalog.sqlite3")) as connection, connection:
        if damage == "missing":
            connection.run("DELETE FROM bound_references")
        else:
            connection.run("UPDATE bound_references SET payload_json='{}'")
    assert InspectHistorySnapshot(root, verify_partitions=True).reason == "history_reference_unavailable"
    supplier.calls.clear()
    result = RunHistorySync(config, supplier, clock=lambda: NOW)
    if damage == "tampered":
        assert result.state == "failed" and result.reason == "history_reference_content_conflict"
        return
    assert result.state == "already_current"
    assert supplier.calls == []
    assert InspectHistorySnapshot(root, verify_partitions=True).state == "active"


def test_new_st_exclusion_filters_old_physical_rows_for_all_consumers(tmp_path):
    from trader.download.domain.history_price_qualification import HISTORY_UNIVERSE_CONTRACT
    from trader.download.infra.published_history_snapshot import SQLitePublishedHistorySnapshot

    dates = (date(2026, 9, 8), date(2026, 9, 9))
    config = HistorySyncConfiguration(
        tmp_path / "baostock",
        sessions=2,
        reread_sessions=2,
        minimum_free_bytes=0,
        reference_root=(tmp_path / "baostock").parent / "reference",
        training_root=(tmp_path / "baostock").parent / "models",
    )
    assert RunHistorySync(config, FakeSupplier(dates, industry="bank"), clock=lambda: NOW).state == "completed"

    class Excluding(FakeSupplier):
        def load_context(self, as_of, sessions):
            context = super().load_context(as_of, sessions)
            return replace(
                context,
                universe=tuple(item for item in context.universe if item.code == "600001"),
                industry_intervals=tuple(item for item in context.industry_intervals if item.code == "600001"),
                st_evidence=tuple(
                    replace(item, status="ever_st") if item.code == "600002" else item for item in context.st_evidence
                ),
                source_versions=replace(context.source_versions, dependency_versions=(HISTORY_UNIVERSE_CONTRACT,)),
            )

    assert RunHistorySync(config, Excluding(dates, industry="bank"), clock=lambda: NOW).state == "completed"
    snapshot = HistoryControlIndex(config.archive_root / "catalog.sqlite3").load_published_state().snapshot
    reader = HistorySnapshotReader(config.archive_root)
    assert {item.code for item in reader.read_day(dates[0], snapshot)} == {"600001"}
    assert reader.count_range(dates[0], dates[-1], snapshot, codes=("600001", "600002")) == len(dates)
    assert reader.count_range(dates[0], dates[-1], snapshot, codes=("600002",)) == 0
    assert reader.read_code_window("600002", dates, snapshot) == ()
    assert tuple(reader.iter_code("600002", dates[0], dates[-1], snapshot)) == ()
    assert {item.code for item in reader.iter_range(dates[0], dates[-1], snapshot)} == {"600001"}
    assert {item.code for item in reader.iter_range_by_code(dates[0], dates[-1], snapshot)} == {"600001"}
    assert {item.code for item in reader.iter_snapshot_revisions(snapshot)} == {"600001"}
    published = SQLitePublishedHistorySnapshot(config.archive_root)
    manifest = published.manifest()
    assert manifest.universe_codes == ("600001",)
    assert [item.code for item in published.read_windows(manifest, ("600001", "600002"), sessions=2)] == ["600001"]
    assert [item.code for item in published.iter_windows(manifest, sessions=2)] == ["600001"]

    with (
        closing(OpenSqliteConnection(config.archive_root / snapshot.partitions[0].relative_path)) as connection,
        connection,
    ):
        assert connection.run("SELECT COUNT(*) FROM daily_records WHERE code='600002'").fetchone()[0] > 0

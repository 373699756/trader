from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, timedelta

import pytest

from tests.unit.infra.research.test_history_archive_sync import NOW, FakeSupplier, _download
from tests.unit.infra.test_history_supplier_router import Baseline, Prices, _current_universe
from trader.download.domain.baostock_daily import BaoStockIndustryInterval
from trader.download.domain.history_reference import HistoryReferenceSnapshot, HistoryStEvidence
from trader.download.domain.history_sync import HistoryStEligibilitySummary, HistorySyncConfiguration
from trader.download.domain.published_history import PublishedHistoryCell, PublishedHistoryWindow
from trader.download.domain.security_eligibility import is_st_security_name
from trader.download.infra.history_archive_reader import SQLiteHistoryArchiveReader
from trader.download.infra.history_archive_sync import run_history_sync
from trader.download.infra.history_control_repository import SQLiteHistoryControlRepository
from trader.download.infra.history_reference_files import (
    history_filter_config_root,
    history_industry_mapping_path,
    history_st_evidence_path,
    read_history_reference,
    write_history_reference,
)
from trader.download.infra.history_st_source import HistoryStNameSource, parse_name_history_st
from trader.download.infra.history_supplier_router import HistorySupplierRouter


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
    assert parse_name_history_st("600848", "上海临港", date(2026, 10, 9), html).status == expected


@pytest.mark.parametrize(
    "text",
    ("maintenance", "<td>证券简称更名历史：</td><td>--</td>", "<td>证券简称更名历史：</td><td>－－</td>"),
)
def test_missing_name_history_never_means_clear(text):
    with pytest.raises(ValueError):
        parse_name_history_st("600848", "上海临港", date(2026, 10, 9), text)


@pytest.mark.parametrize((("current_name", "expected")), (("青岛特锐", "clear"), ("*ST测试", "ever_st")))
def test_explicit_empty_name_history_uses_the_official_current_name(current_name, expected):
    html = '<td>证券简称更名历史：</td><td colspan="3"></td>'

    assert parse_name_history_st("300001", current_name, date(2026, 10, 10), html).status == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    (("ST豆神", True), ("*ST仕净", True), ("ＳＴ公司", True), ("S*ST公司", True), ("正常公司", False)),
)
def test_st_name_rule_normalizes_current_and_historical_evidence(name, expected):
    assert is_st_security_name(name) is expected


def test_st_filter_removes_all_history_for_ever_st_and_unknown_before_baseline(tmp_path):
    dates = (date(2026, 9, 8), date(2026, 9, 9))

    class MixedSt:
        def fetch(self, universe, as_of):
            return tuple(
                HistoryStEvidence(item.code, as_of, status)
                for item, status in zip(universe, ("clear", "ever_st", "unknown"), strict=True)
            )

    class RecordingBaseline(Baseline):
        def load_context(self, as_of, sessions, *, universe):
            assert tuple(item.code for item in universe) == ("600001",)
            return super().load_context(as_of, sessions, universe=universe)

    config = HistorySyncConfiguration(tmp_path, sessions=2, reread_sessions=2, minimum_free_bytes=0)
    baseline, prices = RecordingBaseline(dates), Prices()
    result = run_history_sync(
        config,
        HistorySupplierRouter(baseline, prices, lambda: _current_universe("600001", "600002", "600003"), MixedSt()),
        clock=lambda: NOW,
    )
    assert result.state == "completed"
    assert baseline.calls == []
    published = SQLiteHistoryControlRepository(tmp_path / "control.sqlite3").load_published_state()
    assert tuple(item.code for item in published.universe.securities) == ("600001",)


def test_unknown_price_gap_breaks_training_window_and_resumes_with_tencent(tmp_path):
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

        def fetch_baostock_prices(self, *_args):
            pytest.fail("zero recovery budget must not call BaoStock")

    config = HistorySyncConfiguration(tmp_path, sessions=125, minimum_free_bytes=0, baostock_max_codes=0)
    result = run_history_sync(config, Sparse(dates, industry="bank"), clock=lambda: NOW)
    assert result.state == "completed" and result.unresolved_price_cells == 1
    control = SQLiteHistoryControlRepository(tmp_path / "control.sqlite3")
    snapshot = control.load_state().active_snapshot
    reader = SQLiteHistoryArchiveReader(tmp_path)
    windows = tuple(reader.iter_training_windows(snapshot, dates, {"600001"}))
    assert windows and all(gap not in {point.trade_date for point in window.points} for window in windows)
    assert reader.read_day(gap, snapshot)[0].cell.status == "unknown_missing"
    resumed = FakeSupplier(dates, industry="bank")
    result = run_history_sync(config, resumed, clock=lambda: NOW)
    assert result.state == "completed" and result.unresolved_price_cells == 0
    assert gap in resumed.calls[0][1]
    assert "baostock_prices" not in resumed.events
    assert run_history_sync(config, resumed, clock=lambda: NOW).state == "already_current"


def test_baostock_recovery_cap_and_rotation_leave_explicit_gaps(tmp_path):
    dates = (date(2026, 9, 8), date(2026, 9, 9))

    class Missing(FakeSupplier):
        recovered = []

        def fetch_tencent_window(self, security, requested):
            return PublishedHistoryWindow(
                security.code,
                tuple(PublishedHistoryCell(security.code, day, "unknown_missing", None, None) for day in requested),
            )

        def fetch_baostock_prices(self, security, requested):
            self.recovered.append(security.code)
            return _download(security.code, requested)

    config = HistorySyncConfiguration(
        tmp_path, sessions=2, reread_sessions=2, minimum_free_bytes=0, baostock_max_codes=1
    )
    supplier = Missing(dates)
    first = run_history_sync(config, supplier, clock=lambda: NOW)
    assert first.state == "completed" and first.unresolved_price_cells == 2
    assert supplier.recovered == ["600001"]
    supplier.recovered.clear()
    second = run_history_sync(config, supplier, clock=lambda: NOW)
    assert second.state == "completed"
    assert supplier.recovered == ["600002"]


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
    path = tmp_path / "reference.json"
    write_history_reference(path, reference)
    assert read_history_reference(path) == reference
    payload = json.loads(path.read_text())
    assert payload["st_statuses"] == {code: ["2026-10-09", "clear"]}
    assert payload["industries"][code] == [
        ["2020-01-01", "2023-01-01", "bank", "csrc"],
        ["2023-01-01", None, "finance", "csrc"],
    ]
    assert "st_evidence" not in payload and "industry_intervals" not in payload
    payload["eligible_codes"] = ["600002"]
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        read_history_reference(path)


def test_history_filter_files_use_fixed_names_beside_history(tmp_path):
    history = tmp_path / "data" / "history"

    assert history_filter_config_root(history) == tmp_path / "data" / "filter_config"
    assert history_st_evidence_path(history) == tmp_path / "data" / "filter_config" / "historical_st.json"
    assert history_industry_mapping_path(history) == tmp_path / "data" / "filter_config" / "industry_mapping.json"


def test_st_checkpoints_cache_clear_and_permanently_keep_ever_st(tmp_path):
    from trader.infra.workers import injected_executor

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
        tmp_path / "st.json", get, injected_executor(None), lambda: False, report=lambda *_: None
    )
    universe = _current_universe("600001", "600002")
    as_of = date(2026, 10, 9)
    assert tuple(item.status for item in source.fetch(universe, as_of)) == ("clear", "ever_st")
    assert source.fetch(universe, as_of)
    assert len(calls) == 2
    assert source.fetch(universe, as_of + timedelta(days=1))
    assert len(calls) == 3 and "600001" in calls[-1]


def test_missing_reference_fails_closed_for_training(tmp_path):
    from trader.download.infra.history_archive_reader import HistoryArchiveReadError

    dates = tuple(date(2026, 7, 1) + timedelta(days=i) for i in range(65))
    config = HistorySyncConfiguration(tmp_path, sessions=65, minimum_free_bytes=0)
    assert run_history_sync(config, FakeSupplier(dates, industry="bank"), clock=lambda: NOW).state == "completed"
    snapshot = SQLiteHistoryControlRepository(tmp_path / "control.sqlite3").load_published_state().snapshot
    reader = SQLiteHistoryArchiveReader(tmp_path)
    assert tuple(reader.iter_training_windows(snapshot, dates, {"600001"}))
    history_industry_mapping_path(tmp_path).unlink()
    with pytest.raises(HistoryArchiveReadError, match="history_reference_unavailable"):
        tuple(reader.iter_training_windows(snapshot, dates, {"600001"}))


def test_expired_baostock_budget_leaves_gaps_and_does_not_call_supplier(tmp_path, monkeypatch):
    import trader.download.infra.history_archive_sync as sync

    dates = (date(2026, 9, 8), date(2026, 9, 9))

    class Missing(FakeSupplier):
        def fetch_tencent_window(self, security, requested):
            return PublishedHistoryWindow(
                security.code,
                tuple(PublishedHistoryCell(security.code, day, "unknown_missing", None, None) for day in requested),
            )

        def fetch_baostock_prices(self, *_args):
            pytest.fail("expired recovery budget must not call BaoStock")

    readings = iter((0.0, 200.0))
    monkeypatch.setattr(sync.monotonic_time, "monotonic", lambda: next(readings, 200.0))
    config = HistorySyncConfiguration(tmp_path, sessions=2, reread_sessions=2, minimum_free_bytes=0)
    result = run_history_sync(config, Missing(dates), clock=lambda: NOW)
    assert result.state == "completed" and result.unresolved_price_cells == 4


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
            get,
            executor,
            lambda: False,
            report=lambda *counts: reports.append(counts),
            batch_size=2,
        )
        result = source.fetch(_current_universe("600001", "600002", "600003", "600004"), date(2026, 10, 9))
        assert len(result) == 4 and all(item.status == "clear" for item in result)
        assert reports[0] == (2, 4, None)
        assert reports[1][:2] == (4, 4)
        assert reports[1][2] == HistoryStEligibilitySummary(4, 0, 0)
        assert executor.status().rejected_count == 0
    finally:
        executor.stop(wait=True, cancel_futures=True)


@pytest.mark.parametrize("damage", ("missing", "tampered"))
def test_sync_restores_bound_reference_without_redownloading_prices(tmp_path, damage):
    from trader.download.infra.history_archive_status import inspect_history_archive

    root = tmp_path / "baostock"
    dates = (date(2026, 9, 8), date(2026, 9, 9))
    supplier = FakeSupplier(dates, industry="bank")
    config = HistorySyncConfiguration(root, sessions=2, reread_sessions=2, minimum_free_bytes=0)
    assert run_history_sync(config, supplier, clock=lambda: NOW).state == "completed"
    snapshot = SQLiteHistoryControlRepository(root / "control.sqlite3").load_published_state().snapshot
    SQLiteHistoryArchiveReader(root).reference_index(snapshot)
    path = history_industry_mapping_path(root)
    if damage == "missing":
        path.unlink()
    else:
        path.write_text("{}")
    assert inspect_history_archive(root, verify_partitions=True).reason == "history_reference_unavailable"
    supplier.calls.clear()
    assert run_history_sync(config, supplier, clock=lambda: NOW).state == "already_current"
    assert supplier.calls == []
    assert inspect_history_archive(root, verify_partitions=True).state == "active"


def test_new_st_exclusion_filters_old_physical_rows_for_all_consumers(tmp_path):
    from trader.download.domain.history_price_qualification import HISTORY_UNIVERSE_CONTRACT
    from trader.download.infra.published_history_archive import SQLitePublishedHistoryArchive

    dates = (date(2026, 9, 8), date(2026, 9, 9))
    config = HistorySyncConfiguration(tmp_path / "baostock", sessions=2, reread_sessions=2, minimum_free_bytes=0)
    assert run_history_sync(config, FakeSupplier(dates, industry="bank"), clock=lambda: NOW).state == "completed"

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

    assert run_history_sync(config, Excluding(dates, industry="bank"), clock=lambda: NOW).state == "completed"
    snapshot = SQLiteHistoryControlRepository(config.archive_root / "control.sqlite3").load_published_state().snapshot
    reader = SQLiteHistoryArchiveReader(config.archive_root)
    assert {item.code for item in reader.read_day(dates[0], snapshot)} == {"600001"}
    assert reader.count_range(dates[0], dates[-1], snapshot, codes=("600001", "600002")) == len(dates)
    assert reader.count_range(dates[0], dates[-1], snapshot, codes=("600002",)) == 0
    assert reader.read_code_window("600002", dates, snapshot) == ()
    assert tuple(reader.iter_code("600002", dates[0], dates[-1], snapshot)) == ()
    assert {item.code for item in reader.iter_range(dates[0], dates[-1], snapshot)} == {"600001"}
    assert {item.code for item in reader.iter_range_by_code(dates[0], dates[-1], snapshot)} == {"600001"}
    assert {item.code for item in reader.iter_snapshot_revisions(snapshot)} == {"600001"}
    published = SQLitePublishedHistoryArchive(config.archive_root)
    manifest = published.manifest()
    assert manifest.universe_codes == ("600001",)
    assert [item.code for item in published.read_windows(manifest, ("600001", "600002"), sessions=2)] == ["600001"]
    assert [item.code for item in published.iter_windows(manifest, sessions=2)] == ["600001"]
    import sqlite3

    with sqlite3.connect(config.archive_root / snapshot.partitions[0].relative_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM daily_records WHERE code='600002'").fetchone()[0] > 0

from __future__ import annotations

import hashlib
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import trader.training.infra.history.history_training_due as due_module
from scripts.runtime_diagnostics.history_sqlite_performance import inspect_history_sqlite_performance
from tests.helpers.qfq import publish_windows
from trader.download.application.read_published_history import ReadPublishedHistoryUseCase
from trader.download.domain.baostock_daily import (
    BaoStockCalendar,
    BaoStockCodeBatch,
    BaoStockCodeDownload,
    BaoStockDailyCell,
    BaoStockDailyFact,
    BaoStockDailySide,
    BaoStockIndustryInterval,
    BaoStockSecurity,
    BaoStockSourceVersions,
)
from trader.download.domain.history_control import HistoryActiveSnapshot, HistorySnapshotPartition
from trader.download.domain.history_maintenance import HistoryMaintenanceStatus
from trader.download.domain.history_reference import HistoryStEvidence
from trader.download.domain.history_sync import HistorySupplierContext, HistorySyncConfiguration
from trader.download.domain.published_history import ProjectHistoryCell, PublishedHistoryWindow
from trader.download.infra.history_control_index import HistoryControlIndex
from trader.download.infra.history_prices import HistoryPricePartition
from trader.download.infra.history_snapshot_reader import HistoryPartitionRevisionComparison
from trader.download.infra.history_sync_runner import RunHistorySync
from trader.download.infra.published_history_snapshot import SQLitePublishedHistorySnapshot
from trader.download.infra.qfq_sqlite import SQLiteQfqWindowCache
from trader.infra.sqlite_connection import OpenSqliteConnection
from trader.recommendation.application.ports.market_data import MarketDataUnavailableError
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.infra.market_data.published_history_cache import PublishedHistoryCache
from trader.training.infra.history.history_training_due import (
    EvaluateHistoryTrainingDue,
    HistoryTrainingDueQuery,
    _revised_dates_since_bundle,
)
from trader.training.infra.history.history_training_input import SQLiteHistoryTrainingInputSnapshot
from trader.training.infra.model_bundles.bundle_snapshot import ActiveHeadBundle
from trader.training.infra.profile.v2.contracts import V2_TRAINING_PROFILE
from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE

NOW = datetime(2026, 9, 10, 20, 30, tzinfo=ZoneInfo("Asia/Shanghai"))


def _side(code: str, day: date, adjustment: str) -> BaoStockDailySide:
    return BaoStockDailySide(
        code,
        day,
        adjustment,  # type: ignore[arg-type]
        10.0,
        10.0,
        10.0,
        10.0,
        100.0,
        1000.0,
        10.0 if adjustment == "unadjusted" else None,
        0.0 if adjustment == "unadjusted" else None,
        0.01 if adjustment == "unadjusted" else None,
        "trading",
    )


class _Supplier:
    def __init__(self, dates: tuple[date, ...]) -> None:
        self._dates = dates

    def load_context(self, _as_of: date, sessions: int) -> HistorySupplierContext:
        return HistorySupplierContext(
            BaoStockCalendar(self._dates[-sessions:]),
            (BaoStockSecurity("600001", "样本", "main", date(2000, 1, 1), None, "test"),),
            BaoStockSourceVersions("test", "3.12", ()),
            (BaoStockIndustryInterval("600001", date(2000, 1, 1), None, "银行", "test"),),
            (HistoryStEvidence("600001", _as_of, "clear"),),
        )

    def fetch_code(self, security, dates):
        cells = tuple(
            BaoStockDailyCell(
                security.code,
                day,
                "complete",
                _side(security.code, day, "unadjusted"),
                _side(security.code, day, "qfq"),
            )
            for day in dates
        )
        return BaoStockCodeDownload(
            BaoStockCodeBatch(security.code, cells),
            tuple(BaoStockDailyFact(security.code, day, False) for day in dates),
        )

    def fetch_tencent_window(self, security, dates):
        return PublishedHistoryWindow(
            security.code,
            tuple(ProjectHistoryCell(cell) for cell in self.fetch_code(security, dates).batch.cells),
        )

    def fetch_baostock_raw(self, security, dates):
        value = self.fetch_code(security, dates)
        cells = tuple(replace(cell, qfq=None, status="qfq_missing") for cell in value.batch.cells)
        return BaoStockCodeDownload(BaoStockCodeBatch(security.code, cells), value.daily_facts)

    def fetch_baostock_prices(self, security, dates):
        return self.fetch_code(security, dates)


@pytest.fixture(scope="module")
def prepared_history(tmp_path_factory: pytest.TempPathFactory):
    """Copy a closed seed; every writer receives independent database files."""
    seeds: dict[tuple[date, ...], tuple[Path, HistoryMaintenanceStatus]] = {}

    def prepare(root: Path, dates: tuple[date, ...]) -> HistoryMaintenanceStatus:
        if dates not in seeds:
            seed = tmp_path_factory.mktemp("published-history") / "history"
            configuration = HistorySyncConfiguration(
                seed,
                sessions=len(dates),
                reread_sessions=2,
                minimum_free_bytes=0,
                reference_root=(seed).parent / "reference",
                training_root=(seed).parent / "models",
            )
            result = RunHistorySync(configuration, _Supplier(dates), clock=lambda: NOW)
            assert result.state == "completed"
            seeds[dates] = seed, result
        seed, result = seeds[dates]
        # No hard links: corruption and append tests must not mutate another case.
        shutil.copytree(seed, root)
        return replace(result, archive_root=root)

    return prepare


def test_monthly_training_input_binds_active_snapshot_and_counts_typed_rows(
    tmp_path: Path, monkeypatch, prepared_history
) -> None:
    archive_root = tmp_path / "history"
    dates = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))
    result = prepared_history(archive_root, dates)

    assert result.state == "completed"
    expected_hash = HistoryControlIndex(archive_root / "catalog.sqlite3").load_state().active_snapshot_hash

    def fail_full_history_read(_self):
        raise AssertionError("training input must not load historical checkpoints")

    monkeypatch.setattr(HistoryControlIndex, "load_state", fail_full_history_read)
    archive = SQLiteHistoryTrainingInputSnapshot.open(tmp_path / "history")
    assert archive.snapshot.active_snapshot_hash == expected_hash
    assert len(tuple(archive.iter_training_rows(frozenset(dates)))) == len(dates)
    assert archive.snapshot.label_cutoff == dates[-2]


def test_training_input_streams_rows_from_published_snapshot(tmp_path: Path, prepared_history) -> None:
    archive_root = tmp_path / "history"
    dates = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))
    prepared_history(archive_root, dates)
    archive = SQLiteHistoryTrainingInputSnapshot.open(tmp_path / "history")
    assert archive.training_row_upper_bound(frozenset(dates)) >= 3
    assert len(tuple(archive.iter_training_rows(frozenset(dates)))) == 3


def test_training_reader_uses_narrow_projection_without_rechecking_published_payloads(
    tmp_path: Path, monkeypatch, prepared_history
) -> None:
    archive_root = tmp_path / "history"
    dates = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))
    prepared_history(archive_root, dates)
    monkeypatch.setattr(
        HistoryPricePartition,
        "verify_logical_content",
        classmethod(lambda *_args, **_kwargs: pytest.fail("training must trust the published payload")),
    )
    archive = SQLiteHistoryTrainingInputSnapshot.open(archive_root)
    rows = tuple(archive.iter_training_rows(frozenset(dates)))
    assert len(rows) == len(dates)
    assert all(row.point is not None for row in rows)


def test_training_input_streams_typed_rows_without_per_code_queries(tmp_path: Path, prepared_history) -> None:
    archive_root = tmp_path / "history"
    dates = tuple(date(2026, 1, 1) + timedelta(days=offset) for offset in range(62))
    prepared_history(archive_root, dates)
    archive = SQLiteHistoryTrainingInputSnapshot.open(tmp_path / "history")
    progress: list[int] = []

    rows = tuple(archive.iter_training_rows(frozenset(dates), progress.append))

    assert tuple(row.trade_date for row in rows) == dates
    assert all(row.point is not None and row.industry == "银行" for row in rows)
    assert progress[-1] == len(dates)
    assert archive.training_row_upper_bound(frozenset(dates)) >= progress[-1]


def test_recommendation_reads_the_exact_snapshot_published_by_download(tmp_path: Path, prepared_history) -> None:
    archive_root = tmp_path / "history"
    dates = tuple(date(2026, 1, 1) + timedelta(days=offset) for offset in range(251))
    result = prepared_history(archive_root, dates)
    training = SQLiteHistoryTrainingInputSnapshot.open(archive_root)
    history = PublishedHistoryCache(
        ReadPublishedHistoryUseCase(SQLitePublishedHistorySnapshot(archive_root)),
        lookback_sessions=251,
    )

    assert history.refresh() is True
    loaded = history.load(("600001",))
    status = history.status()

    assert status.snapshot_hash == result.active_snapshot_hash
    assert training.has_industry_window(frozenset(dates), 251)
    assert status.universe_rows == 1
    assert status.covered_rows == 1
    assert len(loaded["600001"]) == 20
    assert history.summaries(loaded, NOW)["600001"].sample_count == 251
    assert history.summaries(loaded, NOW)["600001"].profile.median_amount_20d == 1000.0


def test_recommendation_without_an_active_snapshot_is_pending_and_never_fabricates_history(tmp_path: Path) -> None:
    history = PublishedHistoryCache(
        ReadPublishedHistoryUseCase(SQLitePublishedHistorySnapshot(tmp_path / "history")),
        lookback_sessions=61,
    )
    restrictions: dict[str, set[str]] = {}

    assert history.load(("600001",), action_restrictions=restrictions) == {}
    assert history.status().state == "unavailable"
    assert restrictions == {"600001": {"history_data_pending"}}


@pytest.mark.parametrize("deadline", (None, NOW + timedelta(seconds=20)))
@pytest.mark.parametrize("previous_projection", (False, True))
def test_history_consumers_do_not_wait_for_background_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    previous_projection: bool,
    deadline: datetime | None,
    prepared_history,
) -> None:
    archive_root = tmp_path / "history"
    dates = tuple(date(2026, 1, 1) + timedelta(days=offset) for offset in range(61))
    prepared_history(archive_root, dates)
    archive = SQLitePublishedHistorySnapshot(archive_root)
    manifest = archive.manifest()
    assert manifest is not None
    short = SQLiteQfqWindowCache((tmp_path / "qfq") / "daily_window.sqlite3", "v3")
    window = archive.read_windows(manifest, ("600001",), sessions=61)[0]
    publish_windows(short, (window,), "tencent:fixture")
    history = PublishedHistoryCache(
        ReadPublishedHistoryUseCase(short),
        lookback_sessions=61,
        outcome_history=ReadPublishedHistoryUseCase(archive),
    )
    old_manifest = short.manifest()
    assert old_manifest is not None
    if previous_projection:
        assert history.refresh()
    # Publish a genuine new short-window identity while complete history stays stable.
    publish_windows(short, (window,), "tencent:fixture-updated")
    new_manifest = short.manifest()
    assert new_manifest is not None and new_manifest.snapshot_hash != old_manifest.snapshot_hash
    original_windows = short.iter_windows
    entered, release = threading.Event(), threading.Event()

    def blocked_windows(manifest, *, sessions):
        entered.set()
        assert release.wait(5)
        yield from original_windows(manifest, sessions=sessions)

    monkeypatch.setattr(short, "iter_windows", blocked_windows)
    with ThreadPoolExecutor(max_workers=2) as pool:
        refresh = pool.submit(history.refresh)
        try:
            assert entered.wait(2)
            load = pool.submit(history.load, ("600001",), deadline=deadline)
            if previous_projection:
                assert len(load.result(timeout=2)["600001"]) == 20
                assert history.status().snapshot_hash == old_manifest.snapshot_hash
            else:
                with pytest.raises(MarketDataUnavailableError, match="history_projection_loading"):
                    load.result(timeout=2)
                assert history.status().snapshot_hash is None
            outcomes = pool.submit(history.read_outcome_bars, ("600001",), NOW)
            bars = outcomes.result(timeout=2)["600001"]
            assert len(bars) == 61
            assert bars[-1].trade_date == manifest.data_cutoff.isoformat()
            assert archive.manifest() == manifest
            assert not refresh.done()
            # Online bars must never mask a corrupt independent settlement source.
            (archive_root / "catalog.sqlite3").write_bytes(b"invalid")
            invalid_outcomes = pool.submit(history.read_outcome_bars, ("600001",), NOW)
            assert invalid_outcomes.result(timeout=2) == {}
            assert history.status().error_count == 1
            assert bool(history.cached(("600001",))) is previous_projection
            assert not refresh.done()
        finally:
            release.set()
        assert refresh.result(timeout=2)
    assert history.status().snapshot_hash == new_manifest.snapshot_hash
    assert len(history.load(("600001",))["600001"]) == 20


def test_shared_published_reader_reuses_partition_trust_for_projection_and_outcomes(
    tmp_path, monkeypatch, prepared_history
):
    root = tmp_path / "history"
    dates = tuple(date(2026, 1, 1) + timedelta(days=offset) for offset in range(61))
    prepared_history(root, dates)
    verified = []
    verify = HistoryPricePartition.verify.__func__

    def observed(cls, path, reference, progress=None):
        verified.append(reference)
        return verify(cls, path, reference, progress)

    monkeypatch.setattr(HistoryPricePartition, "verify", classmethod(observed))
    archive = SQLitePublishedHistorySnapshot(tmp_path / "history")
    manifest = archive.manifest()
    history = PublishedHistoryCache(ReadPublishedHistoryUseCase(archive), lookback_sessions=61)
    assert history.refresh()
    initial_count = len(verified)
    assert initial_count > 0
    assert len(archive.read_windows(manifest, ("600001",), sessions=61)[0].cells) == 61
    assert history.read_outcome_bars(("600001",), NOW)
    assert len(verified) == initial_count
    partition = root / verified[0].relative_path
    with closing(OpenSqliteConnection(partition)) as connection, connection:
        connection.run("UPDATE daily_records SET qfq_close=99 WHERE trade_date=?", (dates[0].isoformat(),))
    before = history.entries()
    assert history.read_outcome_bars(("600001",), NOW) == {}
    assert history.entries() == before
    assert history.status().error_count == 1
    assert len(verified) == initial_count + 1


def test_deadline_bound_history_load_never_starts_an_archive_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = SQLitePublishedHistorySnapshot(tmp_path / "history")
    history = PublishedHistoryCache(ReadPublishedHistoryUseCase(archive), lookback_sessions=61)

    def unexpected_manifest():
        pytest.fail("market deadline must not start archive verification")

    monkeypatch.setattr(archive, "manifest", unexpected_manifest)
    restrictions: dict[str, set[str]] = {}
    assert history.load(("600001",), deadline=NOW, action_restrictions=restrictions) == {}
    assert restrictions == {"600001": {"history_data_pending"}}


def test_recommendation_retains_the_last_valid_projection_when_the_archive_becomes_unreadable(
    tmp_path: Path, prepared_history
) -> None:
    archive_root = tmp_path / "history"
    dates = tuple(date(2026, 1, 1) + timedelta(days=offset) for offset in range(61))
    prepared_history(archive_root, dates)
    history = PublishedHistoryCache(
        ReadPublishedHistoryUseCase(SQLitePublishedHistorySnapshot(archive_root)),
        lookback_sessions=61,
    )
    assert history.refresh() is True
    before = history.entries()

    (archive_root / "catalog.sqlite3").write_bytes(b"invalid")

    assert history.refresh() is False
    assert history.entries() == before
    assert history.status().state == "active"
    assert history.status().error_count == 1


def test_history_sqlite_performance_diagnostic_is_bounded_and_never_writes_active_history(
    tmp_path: Path, prepared_history
) -> None:
    archive_root = tmp_path / "history"
    dates = tuple(date(2026, 1, 1) + timedelta(days=offset) for offset in range(70))
    prepared_history(archive_root, dates)
    before = {
        path.relative_to(archive_root): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in archive_root.rglob("*.sqlite3")
    }

    report = inspect_history_sqlite_performance(archive_root, 1, 1, 10)

    after = {
        path.relative_to(archive_root): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in archive_root.rglob("*.sqlite3")
    }
    assert before == after
    assert tuple(item.workload for item in report.queries) == (
        "startup_month_by_code",
        "latest_month",
        "single_code_window",
        "single_day_board",
        "training_projection",
        "active_price_gaps",
    )
    assert report.revision_write.requested_rows == 10
    assert report.revision_write.transaction_count == 1
    assert report.revision_write.seal_ms >= 0


def test_training_due_uses_the_active_snapshot_label_cutoff_and_marks_initial(tmp_path: Path, prepared_history) -> None:
    archive_root = tmp_path / "history"
    dates = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))
    prepared_history(archive_root, dates)

    evaluation = EvaluateHistoryTrainingDue(
        HistoryTrainingDueQuery(archive_root, tmp_path / "train", NOW, V3_TRAINING_PROFILE)
    )

    assert evaluation is not None
    assert evaluation.state.reason == "initial_training_required"
    assert evaluation.state.current_label_cutoff == dates[-2]
    assert evaluation.state.training_due is True


def test_training_due_expands_revision_cache_invalidation_to_the_selected_profile_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prepared_history
) -> None:
    archive_root = tmp_path / "history"
    dates = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))
    prepared_history(archive_root, dates)
    observed_dependency_sessions: list[int] = []

    def calculate_invalidation(
        _calendar: tuple[date, ...],
        _revisions: tuple[date, ...],
        *,
        dependency_sessions: int,
    ) -> tuple[date, ...]:
        observed_dependency_sessions.append(dependency_sessions)
        return ()

    monkeypatch.setattr(
        due_module,
        "CalculateHistoryTrainingCacheInvalidationDates",
        calculate_invalidation,
    )

    evaluation = EvaluateHistoryTrainingDue(
        HistoryTrainingDueQuery(archive_root, tmp_path / "train", NOW, V2_TRAINING_PROFILE)
    )

    assert evaluation is not None
    assert observed_dependency_sessions == [250]


def test_normal_new_label_day_keeps_cadence_instead_of_forcing_snapshot_rebind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_root = tmp_path / "history"
    configuration = HistorySyncConfiguration(
        archive_root,
        sessions=3,
        reread_sessions=2,
        minimum_free_bytes=0,
        reference_root=(archive_root).parent / "reference",
        training_root=(archive_root).parent / "models",
    )
    first_dates = (date(2026, 9, 7), date(2026, 9, 8), date(2026, 9, 9))
    RunHistorySync(configuration, _Supplier(first_dates), clock=lambda: NOW)
    baseline = HistoryControlIndex(archive_root / "catalog.sqlite3").load_state().active_snapshot
    assert baseline is not None
    contract_hash = "9" * 64
    bundle = ActiveHeadBundle(
        tmp_path / "train/tomorrow-v3/model.json",
        Strategy.TOMORROW,
        baseline.content_hash,
        baseline.source_identity_hash,
        baseline.label_cutoff,
        contract_hash,
        "a" * 64,
        "b" * 64,
        "c" * 64,
        "d" * 64,
        "e" * 64,
    )
    later_dates = (*first_dates, date(2026, 9, 10))
    RunHistorySync(configuration, _Supplier(later_dates), clock=lambda: NOW)
    monkeypatch.setattr(due_module, "_active_bundle", lambda _root, _profile, _strategy: (bundle, False))

    evaluation = EvaluateHistoryTrainingDue(
        HistoryTrainingDueQuery(
            archive_root,
            tmp_path / "train",
            NOW,
            V3_TRAINING_PROFILE,
            expected_training_contract_hash=contract_hash,
        )
    )

    assert evaluation is not None
    assert evaluation.state.reason == "not_due"
    assert evaluation.state.matured_label_days_since_training == 1
    assert evaluation.revised_dates == ()


def test_revision_detection_compares_semantic_rows_only_in_changed_months(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dates = tuple(date(2026, 9, day) for day in range(1, 6))
    baseline = HistoryActiveSnapshot(
        1,
        dates[-1],
        dates[-2],
        "a" * 64,
        "b" * 64,
        "c" * 64,
        (HistorySnapshotPartition("prices_2026.sqlite3", "d" * 64, 1, calendar_month=9),),
    )
    active = HistoryActiveSnapshot(
        2,
        dates[-1],
        dates[-2],
        "a" * 64,
        "b" * 64,
        "c" * 64,
        (HistorySnapshotPartition("prices_2026.sqlite3", "e" * 64, 1, calendar_month=9),),
    )

    class _Archive:
        def __init__(self, _root: Path) -> None:
            pass

        def revised_dates(
            self,
            _start: date,
            _end: date,
            comparison: HistoryPartitionRevisionComparison,
        ) -> tuple[date, ...]:
            assert comparison.physical_reference == active.partitions[0]
            assert comparison.before_sequence == baseline.sequence
            assert comparison.after_sequence == active.sequence
            return (dates[2],)

    monkeypatch.setattr("trader.training.infra.history.history_training_due.HistorySnapshotReader", _Archive)

    assert _revised_dates_since_bundle(tmp_path, baseline, active, dates, dates[-2]) == (dates[2],)

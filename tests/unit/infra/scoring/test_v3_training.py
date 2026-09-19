from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from trader.domain.recommendation.model_scoring import TRAINED_HEAD_EXPOSURE_CONTRACT, residualize_exposure
from trader.domain.recommendation.models import Strategy
from trader.download.domain.baostock_daily import (
    BaoStockCalendar,
    build_baostock_training_split,
)
from trader.download.domain.history_control import HistoryTrainingDueState
from trader.download.domain.history_revision import HistoryTrainingPoint, HistoryTrainingWindow
from trader.download.infra.history_archive_repack import HistoryArchiveRepackFenceError
from trader.download.infra.history_control_repository import HistoryMaintenanceAlreadyRunningError
from trader.training.application.tomorrow_training import TomorrowTrainingProgress, TomorrowTrainingWindow
from trader.training.domain.tomorrow_training_input import REQUIRED_DAILY_FIELDS, FrozenDailyInputDescriptor
from trader.training.infra.engine import (
    _cleanup_abandoned_workspaces,
    _mature_label_cutoff,
    _training_contract_hash,
)
from trader.training.infra.history.history_training_due import HistoryTrainingDueQuery
from trader.training.infra.history.history_training_input import HistoryTrainingInputSnapshot
from trader.training.infra.profile.v3.contracts import (
    D25_HEAD_CONTRACT,
    HEAD_CONTRACTS,
    TODAY_HEAD_CONTRACT,
    TOMORROW_HEAD_CONTRACT,
    V3_TRAINING_PROFILE,
)
from trader.training.infra.profile.v3.sample_builder import (
    aligned_sample_dates,
    build_training_samples,
    residualize_sample_day,
    training_alpha_target,
)
from trader.training.infra.profile.v3.training import run_tomorrow_training, run_v3_training
from trader.training.infra.profile.v3.training_sample_repository import (
    SQLiteV3TrainingSampleRepository,
    V3TrainingIndustryCounts,
    V3TrainingSample,
)
from trader.training.infra.sample_builder import TrainingSampleBuildRequest

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _cadence_archive(archive_root: Path) -> SimpleNamespace:
    dates = tuple(date(2020, 1, 1) + timedelta(days=index) for index in range(2_000))
    snapshot = HistoryTrainingInputSnapshot(
        "complete_manifest",
        "a" * 64,
        "1" * 64,
        "2" * 64,
        dates[-1],
        dates[-2],
        BaoStockCalendar(dates),
        ("600000",),
        "4" * 64,
    )
    descriptor = FrozenDailyInputDescriptor(
        manifest_hash=snapshot.active_snapshot_hash,
        source_identity="baostock_daily_core",
        source_cutoff=snapshot.source_cutoff,
        requested_sessions=2_000,
        primary_key=("code", "trade_date"),
        fields=REQUIRED_DAILY_FIELDS,
        raw_qfq_layout="same_row",
        row_hash_algorithm="sha256",
        frozen=True,
    )
    return SimpleNamespace(
        snapshot=snapshot,
        archive_root=archive_root,
        active_snapshot=SimpleNamespace(partitions=(object(), object())),
        describe_frozen_daily_input=lambda: descriptor,
        verify_partitions=lambda _progress=None: None,
        training_row_upper_bound=lambda _dates: 0,
    )


def _due(archive: SimpleNamespace, contract, *, reason: str, trained_age: int = 0) -> SimpleNamespace:
    dates = archive.snapshot.calendar.open_dates
    cutoff = dates[-1 - contract.maturity_sessions]
    baseline = None if reason == "initial_training_required" else dates[dates.index(cutoff) - trained_age]
    state = HistoryTrainingDueState(
        f"due-{contract.strategy.value}",
        reason,
        baseline,
        cutoff,
        trained_age,
        reason == "input_revision_due",
        datetime(2026, 9, 11, 9, 0, tzinfo=SHANGHAI),
    )
    bundle = None
    if baseline is not None:
        bundle = SimpleNamespace(
            training_input_hash=archive.snapshot.active_snapshot_hash,
            label_cutoff=baseline,
            training_contract_hash="e" * 64,
            model_hash="b" * 64,
            report_hash="c" * 64,
        )
    return SimpleNamespace(state=state, bundle=bundle, invalidated_cache_dates=())


def _patch_archive(monkeypatch: pytest.MonkeyPatch, archive: SimpleNamespace) -> None:
    monkeypatch.setattr(
        "trader.training.infra.engine.SQLiteHistoryTrainingInputArchive.open",
        lambda _path: archive,
    )


def test_training_cadence_stops_before_sample_work_on_the_nineteenth_matured_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _cadence_archive(tmp_path / "history" / "baostock")
    _patch_archive(monkeypatch, archive)
    monkeypatch.setattr(
        "trader.training.infra.engine.evaluate_history_training_due",
        lambda *_args, **_kwargs: _due(archive, TOMORROW_HEAD_CONTRACT, reason="not_due", trained_age=19),
    )
    monkeypatch.setattr(
        "trader.training.infra.engine.build_training_samples",
        lambda *_args, **_kwargs: pytest.fail("samples must not be built before cadence is due"),
    )

    result = run_tomorrow_training(tmp_path / "history", tmp_path / "train")

    assert result.status == "not_due"
    assert result.matured_label_days_since_training == 19
    assert result.training_due is False


def test_current_training_result_preserves_active_bundle_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _cadence_archive(tmp_path / "history" / "baostock")
    _patch_archive(monkeypatch, archive)
    monkeypatch.setattr(
        "trader.training.infra.engine.evaluate_history_training_due",
        lambda *_args, **_kwargs: _due(archive, TOMORROW_HEAD_CONTRACT, reason="not_due"),
    )

    result = run_tomorrow_training(tmp_path / "history", tmp_path / "train")

    assert result.status == "already_current"
    assert result.model_hash == "b" * 64
    assert result.report_hash == "c" * 64


def test_v3_training_validates_and_scans_history_once_then_fits_heads_sequentially(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _cadence_archive(tmp_path / "history" / "baostock")
    verify_calls = 0

    def verify(progress=None) -> None:
        nonlocal verify_calls
        verify_calls += 1
        if progress is not None:
            progress(2, 2, 1, 100, 100, "row_count")

    archive.verify_partitions = verify
    _patch_archive(monkeypatch, archive)
    monkeypatch.setattr(
        "trader.training.infra.engine.evaluate_history_training_due",
        lambda query: _due(
            archive,
            next(contract for contract in HEAD_CONTRACTS if contract.strategy is query.strategy),
            reason="initial_training_required",
        ),
    )
    scan_calls = 0

    def build_samples(request: TrainingSampleBuildRequest) -> None:
        nonlocal scan_calls
        scan_calls += 1
        sample_day = request.window.split.model_fit_dates[0]
        request.repository.add_final(
            (
                V3TrainingSample(
                    "600000",
                    sample_day,
                    "main",
                    "银行",
                    1.0,
                    (0.0,) * 6,
                    (0.01, 0.02, 0.03, 0.04, 0.05, 0.035),
                ),
            )
        )
        request.repository.prepare_for_model_fitting(request.window.split)

    monkeypatch.setattr("trader.training.infra.engine.build_training_samples", build_samples)
    fitted: list[Strategy] = []

    def fit(_samples, _split, contract, *, progress):
        del progress
        fitted.append(contract.strategy)
        width = len(contract.feature_positions)
        return ({"银行": _industry_model(width)}, 1, 1)

    monkeypatch.setattr("trader.training.infra.engine.fit_industry_models", fit)
    published: list[tuple[Strategy, Path]] = []

    def publish(staging, output, strategy, _profile, _identity) -> None:
        assert {path.name for path in staging.iterdir()} == {"model.json", "report.json", "training-input.json"}
        published.append((strategy, output))

    monkeypatch.setattr("trader.training.infra.engine.publish_head_bundle", publish)

    result = run_v3_training(tmp_path / "history", tmp_path / "train")

    assert result.status == "engineering_ready"
    assert verify_calls == scan_calls == 1
    assert fitted == [Strategy.TODAY, Strategy.TOMORROW, Strategy.D25]
    assert published == [
        (Strategy.TODAY, tmp_path / "train" / "v3" / "today"),
        (Strategy.TOMORROW, tmp_path / "train" / "v3" / "tomorrow"),
        (Strategy.D25, tmp_path / "train" / "v3" / "d25"),
    ]
    assert all(head.status == "engineering_ready" for head in result.heads)
    assert all(head.training_due is False for head in result.heads)


def test_v3_head_maturity_and_contract_hashes_are_independent_and_stable(tmp_path: Path) -> None:
    snapshot = _cadence_archive(tmp_path / "history" / "baostock").snapshot

    assert _mature_label_cutoff(snapshot, TODAY_HEAD_CONTRACT) == snapshot.calendar.open_dates[-2]
    assert _mature_label_cutoff(snapshot, TOMORROW_HEAD_CONTRACT) == snapshot.calendar.open_dates[-2]
    assert _mature_label_cutoff(snapshot, D25_HEAD_CONTRACT) == snapshot.calendar.open_dates[-6]
    hashes = {_training_contract_hash(V3_TRAINING_PROFILE, contract) for contract in HEAD_CONTRACTS}
    assert len(hashes) == 3
    assert _training_contract_hash(V3_TRAINING_PROFILE, TOMORROW_HEAD_CONTRACT) == _training_contract_hash(
        V3_TRAINING_PROFILE, TOMORROW_HEAD_CONTRACT
    )


def test_v3_training_only_fits_the_head_whose_own_cadence_is_due(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _cadence_archive(tmp_path / "history" / "baostock")
    _patch_archive(monkeypatch, archive)

    def due_for_head(query: HistoryTrainingDueQuery):
        contract = next(item for item in HEAD_CONTRACTS if item.strategy is query.strategy)
        if contract.strategy is Strategy.D25:
            return _due(archive, contract, reason="initial_training_required")
        return _due(archive, contract, reason="not_due", trained_age=1)

    monkeypatch.setattr("trader.training.infra.engine.evaluate_history_training_due", due_for_head)

    def build_samples(request: TrainingSampleBuildRequest) -> None:
        request.repository.add_final(
            (
                V3TrainingSample(
                    "600000",
                    request.window.split.model_fit_dates[0],
                    "main",
                    "银行",
                    1.0,
                    (0.0,) * 6,
                    (0.01, 0.02, 0.03, 0.04, 0.05, 0.035),
                ),
            )
        )
        request.repository.prepare_for_model_fitting(request.window.split)

    monkeypatch.setattr("trader.training.infra.engine.build_training_samples", build_samples)
    fitted: list[Strategy] = []

    def fit(_samples, _split, contract, *, progress):
        del progress
        fitted.append(contract.strategy)
        return ({"银行": _industry_model(len(contract.feature_positions))}, 1, 1)

    monkeypatch.setattr("trader.training.infra.engine.fit_industry_models", fit)
    monkeypatch.setattr("trader.training.infra.engine.publish_head_bundle", lambda *_args, **_kwargs: None)

    result = run_v3_training(tmp_path / "history", tmp_path / "train")

    assert fitted == [Strategy.D25]
    assert [head.status for head in result.heads] == ["not_due", "not_due", "engineering_ready"]


def test_training_keeps_each_due_baseline_when_the_shared_scan_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _cadence_archive(tmp_path / "history" / "baostock")
    _patch_archive(monkeypatch, archive)
    monkeypatch.setattr(
        "trader.training.infra.engine.evaluate_history_training_due",
        lambda query: _due(
            archive,
            next(contract for contract in HEAD_CONTRACTS if contract.strategy is query.strategy),
            reason="cadence_due",
            trained_age=20,
        ),
    )
    monkeypatch.setattr(
        "trader.training.infra.engine.build_training_samples",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("forced failure")),
    )

    result = run_v3_training(tmp_path / "history", tmp_path / "train")

    assert result.status == "blocked"
    assert all(head.training_due_reason == "cadence_due" for head in result.heads)
    assert all(head.matured_label_days_since_training == 20 for head in result.heads)
    assert all(head.training_due for head in result.heads)


def test_training_does_not_open_a_second_snapshot_while_history_maintenance_is_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _cadence_archive(tmp_path / "history" / "baostock")
    open_calls: list[Path] = []

    def open_archive(path: Path) -> SimpleNamespace:
        open_calls.append(path)
        return archive

    class BusyMaintenanceLock:
        def __init__(self, _path: Path) -> None:
            pass

        def __enter__(self) -> None:
            raise HistoryMaintenanceAlreadyRunningError("busy")

        def __exit__(self, *_args: object) -> None:
            pass

    monkeypatch.setattr("trader.training.infra.engine.SQLiteHistoryTrainingInputArchive.open", open_archive)
    monkeypatch.setattr("trader.training.infra.engine.HistoryMaintenanceLock", BusyMaintenanceLock)

    result = run_v3_training(tmp_path / "history", tmp_path / "train")

    assert all(head.failure_reasons == ("history_maintenance_running",) for head in result.heads)
    assert open_calls == [tmp_path / "history"]


def test_training_respects_the_history_repack_fence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive = _cadence_archive(tmp_path / "history" / "baostock")
    _patch_archive(monkeypatch, archive)
    monkeypatch.setattr(
        "trader.training.infra.engine.require_history_archive_repack_inactive",
        lambda _root: (_ for _ in ()).throw(HistoryArchiveRepackFenceError("fenced")),
    )

    result = run_v3_training(tmp_path / "history", tmp_path / "train")

    assert all(head.failure_reasons == ("history_archive_repack_activation_pending",) for head in result.heads)


def test_training_cleanup_removes_only_owned_abandoned_workspaces(tmp_path: Path) -> None:
    abandoned = tmp_path / ".training-sample-workspace.abandoned"
    staging = tmp_path / "today" / ".bundle-staging.abandoned"
    preserved = tmp_path / "tomorrow" / "model.json"
    abandoned.mkdir()
    staging.mkdir(parents=True)
    preserved.parent.mkdir(parents=True)
    preserved.touch()

    _cleanup_abandoned_workspaces(tmp_path)

    assert not abandoned.exists()
    assert not staging.exists()
    assert preserved.is_file()


def test_sample_building_creates_t1_through_t5_and_d25_from_one_stream(tmp_path: Path) -> None:
    dates = tuple(date(2021, 1, 1) + timedelta(days=index) for index in range(1_250))
    split = build_baostock_training_split(dates, parent_manifest_hash="a" * 64)
    rows = tuple(_training_point(day, 10.0 + index) for index, day in enumerate(dates[:66]))

    class Archive:
        snapshot = SimpleNamespace(calendar=BaoStockCalendar(dates))

        @staticmethod
        def training_row_upper_bound(_dates) -> int:
            return 66

        @staticmethod
        def iter_training_windows(_dates, progress, *, window_sessions=61):
            assert window_sessions == 61
            for offset in range(6):
                yield _training_window(rows[offset : offset + 61])
            progress(66)

    with SQLiteV3TrainingSampleRepository(tmp_path / "samples.sqlite3") as repository:
        build_training_samples(Archive(), ("600000",), TomorrowTrainingWindow(split), repository)  # type: ignore[arg-type]
        row = repository._connection.execute(
            "SELECT trade_date, target_t1, target_t2, target_t3, target_t4, target_t5, target_d25_aggregate "
            "FROM samples WHERE trade_date=?",
            (dates[60].isoformat(),),
        ).fetchone()

        assert row is not None
        assert row[0] == dates[60].isoformat()
        assert row[1:] == pytest.approx((0.0,) * 6)
        assert "raw_samples" not in {
            item[0] for item in repository._connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }


def test_training_window_and_sample_dates_preserve_the_frozen_tomorrow_contract() -> None:
    dates = tuple(date(2021, 1, 1) + timedelta(days=index) for index in range(1_250))
    split = build_baostock_training_split(dates, parent_manifest_hash="a" * 64)
    window = TomorrowTrainingWindow(split)

    assert window.readable_dates.isdisjoint(split.point_in_time_holdout_dates)
    with pytest.raises(ValueError, match="point-in-time holdout"):
        window.require_readable((split.point_in_time_holdout_dates[0],))
    sample_day, next_day, indices = aligned_sample_dates(dates[:100], set(dates[:100]), frozenset(dates[:100]))[0]
    assert sample_day == dates[60]
    assert next_day == dates[61]
    assert indices == (60, 59, 57, 55, 40, 20, 0)


def test_training_uses_shared_exposure_and_pre_cost_target_contracts() -> None:
    momenta = ((1.0, 10.0), (3.0, 8.0), (2.0, 6.0), (8.0, 4.0))
    boards = ("main", "main", "star", "star")
    industries = ("bank", "software", "bank", "software")
    amounts = (10.0, 20.0, 15.0, 30.0)
    result = residualize_sample_day(momenta, boards, industries, amounts)
    expected = tuple(
        residualize_exposure(
            tuple(row[offset] for row in momenta),
            boards,
            amounts,
            industries=industries,
            contract=TRAINED_HEAD_EXPOSURE_CONTRACT,
        )
        for offset in range(2)
    )

    assert result == expected
    assert training_alpha_target(next_return=0.06, benchmark_return=0.01) == pytest.approx(0.05)


def test_model_progress_keeps_real_industry_count_for_each_head() -> None:
    from trader.training.infra.profile.v3.model_fitting import fit_industry_models

    dates = tuple(date(2021, 1, 1) + timedelta(days=index) for index in range(1_250))
    split = build_baostock_training_split(dates, parent_manifest_hash="a" * 64)

    class Samples:
        @staticmethod
        def require_split(_split) -> None:
            pass

        @staticmethod
        def industry_counts(_contract) -> tuple[V3TrainingIndustryCounts, ...]:
            return (
                V3TrainingIndustryCounts("银行", 0, 0, 0, 0),
                V3TrainingIndustryCounts("软件", 0, 0, 0, 0),
            )

        @staticmethod
        def split_count(_split_name, _contract) -> int:
            return 0

    updates: list[TomorrowTrainingProgress] = []
    progress = SimpleNamespace(publish=updates.append)

    models, training_rows, validation_rows = fit_industry_models(
        Samples(),
        split,
        TODAY_HEAD_CONTRACT,
        progress=progress,  # type: ignore[arg-type]
    )

    assert models == {}
    assert training_rows == validation_rows == 0
    assert [(item.state, item.completed_units, item.total_units) for item in updates] == [
        ("started", 0, 2),
        ("running", 1, 2),
        ("completed", 2, 2),
    ]
    assert all(item.strategy is Strategy.TODAY for item in updates)


def test_training_progress_rejects_impossible_counts() -> None:
    progress = TomorrowTrainingProgress("history_conversion", "running", 50, 100, produced_units=40)
    assert progress.produced_units == 40
    with pytest.raises(ValueError, match="counts"):
        TomorrowTrainingProgress("history_conversion", "running", 101, 100)


def _industry_model(width: int) -> dict[str, object]:
    return {
        "transformer_means": [0.0] * width,
        "transformer_scales": [1.0] * width,
        "ridge_intercept": 0.0,
        "ridge_coefficients": [0.1] * width,
        "lightgbm_model": "fixture",
        "lightgbm_best_iteration": 1,
        "calibration_intercept": 0.0,
        "calibration_slope": 1.0,
        "training_rows": 1,
        "validation_rows": 1,
    }


def _training_point(day: date, close: float) -> HistoryTrainingPoint:
    return HistoryTrainingPoint(day, close, 1_000.0)


def _training_window(points: tuple[HistoryTrainingPoint, ...]) -> HistoryTrainingWindow:
    return HistoryTrainingWindow("600000", "main", "银行", False, "trading", points)

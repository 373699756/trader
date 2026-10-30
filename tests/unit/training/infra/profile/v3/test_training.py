from __future__ import annotations

import json
import sqlite3
from contextlib import nullcontext
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from trader.download.domain.baostock_daily import BaoStockCalendar
from trader.download.domain.history_revision import HistoryTrainingPoint, HistoryTrainingRow
from trader.download.infra.history_control_index import HistoryMaintenanceAlreadyRunningError
from trader.download.infra.history_partition_repack import HistoryPartitionRepackFenceError
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.residualization import TRAINED_HEAD_EXPOSURE_CONTRACT, ResidualizeExposure
from trader.training.application.tomorrow_training import TomorrowTrainingProgress, TomorrowTrainingWindow
from trader.training.domain.tomorrow_training_input import REQUIRED_DAILY_FIELDS, FrozenDailyInputDescriptor
from trader.training.domain.training_dates import BuildTrainingDateSplit
from trader.training.infra.engine import (
    _cleanup_abandoned_workspaces,
    _mature_label_cutoff,
    _training_contract_hash,
)
from trader.training.infra.history.history_training_input import HistoryTrainingInputSnapshot
from trader.training.infra.model_bundles.bundle_codec import TrainedLightGBMModelSnapshot, TrainedRidgeModelSnapshot
from trader.training.infra.model_evaluation import CalibrationResult, CalibrationUnavailableError, PredictionMetrics
from trader.training.infra.model_fitting import FitIndustryModels, RidgeFittingParameters
from trader.training.infra.model_fitting_results import FittedIndustryModel, IndustryFitResult
from trader.training.infra.profile.v2.contracts import V2_TRAINING_PROFILE
from trader.training.infra.profile.v2.training import RunV2Training as _run_v2_training
from trader.training.infra.profile.v3.contracts import (
    D25_HEAD_CONTRACT,
    HEAD_CONTRACTS,
    TOMORROW_HEAD_CONTRACT,
    V3_TRAINING_PROFILE,
)
from trader.training.infra.profile.v3.sample_builder import (
    AlignedSampleDates,
    BuildTrainingSamples,
    ResidualizeSampleDay,
    TrainingAlphaTarget,
)
from trader.training.infra.profile.v3.training import (
    RunTomorrowTraining as _run_tomorrow_training,
)
from trader.training.infra.profile.v3.training import RunV3Training as _run_v3_training
from trader.training.infra.profile.v3.training_sample_index import (
    SQLiteV3TrainingSampleIndex,
    V3TrainingIndustryCounts,
    V3TrainingSample,
)
from trader.training.infra.sample_builder import TrainingSampleBuildRequest, _finalize_sample_day, _PendingSample
from trader.training.infra.training_settings import LoadProfileTrainingSettings

PROJECT_ROOT = Path(__file__).resolve().parents[6]


def run_v2_training(history_root: Path, train_root: Path, **kwargs):
    return _run_v2_training(
        history_root,
        train_root,
        settings=LoadProfileTrainingSettings(PROJECT_ROOT, "v2"),
        sample_root=(train_root).parent / "samples",
        **kwargs,
    )


def run_v3_training(history_root: Path, train_root: Path, **kwargs):
    return _run_v3_training(
        history_root,
        train_root,
        settings=LoadProfileTrainingSettings(PROJECT_ROOT, "v3"),
        sample_root=(train_root).parent / "samples",
        **kwargs,
    )


def run_tomorrow_training(history_root: Path, train_root: Path, **kwargs):
    return _run_tomorrow_training(
        history_root,
        train_root,
        settings=LoadProfileTrainingSettings(PROJECT_ROOT, "v3"),
        sample_root=(train_root).parent / "samples",
        **kwargs,
    )


def _cadence_history(history_root: Path) -> SimpleNamespace:
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
        manifest_hash=snapshot.snapshot_hash,
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
        history_root=history_root,
        published_snapshot=SimpleNamespace(partitions=(object(), object())),
        describe_frozen_daily_input=lambda: descriptor,
        training_row_upper_bound=lambda _dates: 0,
        has_industry_window=lambda _dates, _sessions, **_kwargs: True,
        industry_sample_upper_bounds=lambda *_args: (("fixture", 100_000),),
    )


def _patch_history(monkeypatch: pytest.MonkeyPatch, history: SimpleNamespace) -> None:
    monkeypatch.setattr(
        "trader.training.infra.engine.SQLiteHistoryTrainingInputSnapshot.open",
        lambda _path: history,
    )


def _populate_samples(request: TrainingSampleBuildRequest) -> None:
    split = request.window.split
    days = (
        split.model_fit_dates
        + split.early_stopping_dates
        + split.calibration_dates
        + split.confirmation_dates
        + split.daily_proxy_holdout_dates
    )
    width = max(max(head.feature_positions) for head in request.profile.heads) + 1
    request.index.add_final(
        V3TrainingSample("600000", day, "main", "银行", 1.0, (0.0,) * width, (0.01, 0.02, 0.03, 0.04, 0.05, 0.035))
        for day in days
    )
    request.index.prepare_for_model_fitting(split)


def test_manual_training_does_not_compare_revisions_or_cadence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    history = _cadence_history(tmp_path / "history")
    _patch_history(monkeypatch, history)
    monkeypatch.setattr(
        "trader.training.infra.history.history_training_due.EvaluateHistoryTrainingDue",
        lambda *_args, **_kwargs: pytest.fail("manual training must not evaluate cadence or revisions"),
    )
    monkeypatch.setattr(
        "trader.training.infra.engine.BuildTrainingSamples",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("scan reached")),
    )

    result = run_tomorrow_training(tmp_path / "history", tmp_path / "train")

    assert result.status == "blocked"
    assert result.failure_reasons == ("profile_training_failed",)
    assert result.run_id is not None


def test_training_rejects_empty_samples_instead_of_reusing_existing_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    history = _cadence_history(tmp_path / "history")
    _patch_history(monkeypatch, history)
    monkeypatch.setattr(
        "trader.training.infra.engine.BuildTrainingSamples",
        lambda *_args, **_kwargs: None,
    )

    result = run_tomorrow_training(tmp_path / "history", tmp_path / "train")

    assert result.status == "blocked"
    assert result.failure_reasons == ("profile_training_rows_empty",)
    assert result.model_hash == result.report_hash == ""


@pytest.mark.parametrize("run_training", (run_v2_training, run_v3_training))
def test_sample_sqlite_failure_returns_both_heads_and_releases_locks(tmp_path, monkeypatch, run_training):
    history = _cadence_history(tmp_path / "history")
    _patch_history(monkeypatch, history)

    def fail_samples(_request, *, aligned_rows=None):
        raise sqlite3.OperationalError("fixture disk full with private supplier details")

    monkeypatch.setattr("trader.training.infra.engine.BuildTrainingSamples", fail_samples)
    result = run_training(tmp_path / "history", tmp_path / "train")
    assert result.status == "blocked"
    assert len(result.heads) == 2
    assert all(head.failure_reasons == ("training_database_failed",) for head in result.heads)
    assert all(not head.model_hash and not head.report_hash for head in result.heads)
    assert result.sample_database_peak_bytes > 0
    assert not tuple((tmp_path / "samples").glob("*/complete.json"))
    monkeypatch.setattr("trader.training.infra.engine.BuildTrainingSamples", lambda _request, **_kwargs: None)
    retry = run_training(tmp_path / "history", tmp_path / "train")
    assert all(head.failure_reasons == ("profile_training_rows_empty",) for head in retry.heads)


def test_missing_industry_in_fit_split_blocks_before_history_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    history = _cadence_history(tmp_path / "history")
    history.has_industry_window = lambda _dates, _sessions, **kwargs: kwargs.get("anchor_dates") is None
    _patch_history(monkeypatch, history)
    result = run_tomorrow_training(tmp_path / "history", tmp_path / "train")
    assert result.status == "blocked"
    assert result.failure_reasons == ("history_industry_split_coverage_insufficient",)


@pytest.mark.parametrize("failure", (ValueError, sqlite3.OperationalError))
def test_fitting_failure_reuses_only_complete_samples_on_retry(tmp_path, monkeypatch, failure):
    run_training = run_v3_training
    profile = "v3"
    history = _cadence_history(tmp_path / "history")
    _patch_history(monkeypatch, history)
    builds = 0

    def build_samples(request, *, aligned_rows=None):
        nonlocal builds
        builds += 1
        _populate_samples(request)

    def fail(*_args, **_kwargs):
        raise failure("fixture_fit_failed")

    monkeypatch.setattr("trader.training.infra.engine.BuildTrainingSamples", build_samples)
    monkeypatch.setattr("trader.training.infra.engine.FitIndustryModels", fail)
    first = run_training(tmp_path / "history", tmp_path / "train")
    assert first.status == "blocked"
    assert first.sample_database_peak_bytes > 0
    assert (tmp_path / "samples" / profile / "complete.json").is_file()
    if failure is sqlite3.OperationalError:
        assert all(head.failure_reasons == ("training_database_failed",) for head in first.heads)

    def fit(_samples, _split, contracts, algorithm, *_args, **_kwargs):
        return tuple(_fitted_result(len(contract.feature_positions), algorithm) for contract in contracts)

    monkeypatch.setattr("trader.training.infra.engine.FitIndustryModels", fit)
    monkeypatch.setattr("trader.training.infra.engine.PublishProfileBundles", lambda *_args: None)
    monkeypatch.setattr(
        "trader.training.infra.engine.BuildTrainingSamples",
        lambda _request, **_kwargs: pytest.fail("complete sample cache must skip the history scan"),
    )
    second = run_training(tmp_path / "history", tmp_path / "train")
    assert second.status == "engineering_ready" and builds == 1
    assert not (tmp_path / "samples").exists()


def test_provably_insufficient_industry_rows_fail_before_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    history = _cadence_history(tmp_path / "history")
    history.industry_sample_upper_bounds = lambda *_args: (("银行", 19999),)
    _patch_history(monkeypatch, history)
    monkeypatch.setattr(
        "trader.training.infra.engine.BuildTrainingSamples", lambda *_args: pytest.fail("must fail early")
    )
    result = run_tomorrow_training(tmp_path / "history", tmp_path / "train")
    assert result.status == "blocked"
    assert result.failure_reasons == ("history_industry_training_rows_insufficient",)


def test_training_rejects_insufficient_industry_history_before_sample_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    history = _cadence_history(tmp_path / "history")
    history.has_industry_window = lambda _dates, _sessions: False
    _patch_history(monkeypatch, history)
    monkeypatch.setattr(
        "trader.training.infra.engine.BuildTrainingSamples",
        lambda *_args, **_kwargs: pytest.fail("industry preflight must precede the history scan"),
    )

    result = run_tomorrow_training(tmp_path / "history", tmp_path / "train")

    assert result.status == "blocked"
    assert result.failure_reasons == ("history_industry_coverage_insufficient",)
    assert result.model_hash == result.report_hash == ""


def test_v2_2000_day_budget_reaches_sample_scan_with_full_fit_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_history(monkeypatch, _cadence_history(tmp_path / "history"))
    monkeypatch.setattr(
        "trader.training.infra.engine.BuildTrainingSamples",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("scan reached")),
    )

    result = run_v2_training(tmp_path / "history", tmp_path / "train")

    assert result.status == "blocked"
    assert all(head.failure_reasons == ("profile_training_failed",) for head in result.heads)


def test_v3_training_scans_history_once_then_fits_heads_sequentially(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    history = _cadence_history(tmp_path / "history")
    _patch_history(monkeypatch, history)
    scan_calls = 0

    def build_samples(request: TrainingSampleBuildRequest, *, aligned_rows=None) -> None:
        nonlocal scan_calls
        scan_calls += 1
        _populate_samples(request)

    monkeypatch.setattr("trader.training.infra.engine.BuildTrainingSamples", build_samples)
    fitted: list[Strategy] = []

    def fit(_samples, _split, contracts, algorithm, *_args, progress):
        del progress
        fitted.extend(contract.strategy for contract in contracts)
        return tuple(_fitted_result(len(contract.feature_positions), algorithm) for contract in contracts)

    monkeypatch.setattr("trader.training.infra.engine.FitIndustryModels", fit)
    published: list[tuple[Strategy, Path]] = []

    def publish(output, _profile, staged) -> None:
        for strategy, staging in staged:
            assert {path.name for path in staging.iterdir()} == {"model.json", "metadata.json"}
            metadata = json.loads((staging / "metadata.json").read_text(encoding="utf-8"))
            report = metadata["validation_report"]
            assert report["artifact_structure_valid"] is True
            assert report["artifact_structure_failure_reasons"] == []
            assert report["prediction_return_validation_passed"] is False
            assert report["prediction_return_failure_reasons"] == ["daily_close_proxy_not_point_in_time"]
            assert "validation_passed" not in report
            published.append((strategy, output))

    monkeypatch.setattr("trader.training.infra.engine.PublishProfileBundles", publish)

    result = run_v3_training(tmp_path / "history", tmp_path / "train")

    assert result.status == "engineering_ready"
    assert scan_calls == 1
    assert fitted == [Strategy.TOMORROW, Strategy.D25]
    assert published == [
        (Strategy.TOMORROW, tmp_path / "train" / "v3"),
        (Strategy.D25, tmp_path / "train" / "v3"),
    ]
    assert all(head.status == "engineering_ready" for head in result.heads)
    assert all(head.label_cutoff is not None for head in result.heads)

    repeated = run_v3_training(tmp_path / "history", tmp_path / "train")

    assert repeated.status == "engineering_ready"
    assert scan_calls == 2
    assert fitted == [Strategy.TOMORROW, Strategy.D25] * 2
    assert published[2:] == published[:2]
    assert repeated.heads == result.heads


def test_v3_head_maturity_and_contract_hashes_are_independent_and_stable(tmp_path: Path) -> None:
    snapshot = _cadence_history(tmp_path / "history").snapshot

    assert _mature_label_cutoff(snapshot, TOMORROW_HEAD_CONTRACT) == snapshot.calendar.open_dates[-2]
    assert _mature_label_cutoff(snapshot, D25_HEAD_CONTRACT) == snapshot.calendar.open_dates[-6]
    settings = LoadProfileTrainingSettings(PROJECT_ROOT, "v3")
    hashes = {_training_contract_hash(V3_TRAINING_PROFILE, contract, settings) for contract in HEAD_CONTRACTS}
    assert len(hashes) == 2
    assert _training_contract_hash(V3_TRAINING_PROFILE, TOMORROW_HEAD_CONTRACT, settings) == _training_contract_hash(
        V3_TRAINING_PROFILE, TOMORROW_HEAD_CONTRACT, settings
    )


def test_v3_training_always_fits_both_heads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    history = _cadence_history(tmp_path / "history")
    _patch_history(monkeypatch, history)

    def build_samples(request: TrainingSampleBuildRequest, *, aligned_rows=None) -> None:
        _populate_samples(request)

    monkeypatch.setattr("trader.training.infra.engine.BuildTrainingSamples", build_samples)
    fitted: list[Strategy] = []

    def fit(_samples, _split, contracts, algorithm, *_args, progress):
        del progress
        fitted.extend(contract.strategy for contract in contracts)
        return tuple(_fitted_result(len(contract.feature_positions), algorithm) for contract in contracts)

    monkeypatch.setattr("trader.training.infra.engine.FitIndustryModels", fit)
    monkeypatch.setattr("trader.training.infra.engine.PublishProfileBundles", lambda *_args, **_kwargs: None)

    result = run_v3_training(tmp_path / "history", tmp_path / "train")

    assert fitted == [Strategy.TOMORROW, Strategy.D25]
    assert [head.status for head in result.heads] == ["engineering_ready", "engineering_ready"]


def test_training_reports_both_heads_when_the_shared_scan_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    history = _cadence_history(tmp_path / "history")
    _patch_history(monkeypatch, history)
    monkeypatch.setattr(
        "trader.training.infra.engine.BuildTrainingSamples",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("forced failure")),
    )

    result = run_v3_training(tmp_path / "history", tmp_path / "train")

    assert result.status == "blocked"
    assert all(head.status == "blocked" for head in result.heads)
    assert all(head.failure_reasons == ("profile_training_failed",) for head in result.heads)
    assert [head.label_cutoff for head in result.heads] == [
        history.snapshot.calendar.open_dates[-2],
        history.snapshot.calendar.open_dates[-6],
    ]


def test_training_does_not_open_a_second_snapshot_while_history_maintenance_is_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    history = _cadence_history(tmp_path / "history")
    open_calls: list[Path] = []

    def open_history(path: Path) -> SimpleNamespace:
        open_calls.append(path)
        return history

    class BusyMaintenanceLock:
        def __init__(self, path: Path) -> None:
            self._path = path

        def __enter__(self) -> None:
            if self._path.name == ".history.lock":
                raise HistoryMaintenanceAlreadyRunningError("busy")

        def __exit__(self, *_args: object) -> None:
            pass

    monkeypatch.setattr("trader.training.infra.engine.SQLiteHistoryTrainingInputSnapshot.open", open_history)
    monkeypatch.setattr("trader.training.infra.engine.HistoryMaintenanceLock", BusyMaintenanceLock)

    result = run_v3_training(tmp_path / "history", tmp_path / "train")

    assert all(head.failure_reasons == ("history_maintenance_running",) for head in result.heads)
    assert open_calls == [tmp_path / "history"]


def test_training_respects_the_history_repack_fence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    history = _cadence_history(tmp_path / "history")
    _patch_history(monkeypatch, history)
    monkeypatch.setattr(
        "trader.training.infra.engine.RequireHistoryRepackIdle",
        lambda _root: (_ for _ in ()).throw(HistoryPartitionRepackFenceError("fenced")),
    )

    result = run_v3_training(tmp_path / "history", tmp_path / "train")

    assert all(head.failure_reasons == ("history_archive_repack_activation_pending",) for head in result.heads)


def test_training_cleanup_removes_only_owned_abandoned_workspaces(tmp_path: Path) -> None:
    abandoned = tmp_path / ".training-sample-workspace.abandoned"
    staging = tmp_path / ".bundle-staging.abandoned"
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
    dates = tuple(date(2021, 1, 1) + timedelta(days=index) for index in range(1_600))
    split = BuildTrainingDateSplit(dates, parent_manifest_hash="a" * 64)
    rows = tuple(_training_point(day, 10.0 + index) for index, day in enumerate(dates[:66]))

    class HistoryInput:
        snapshot = SimpleNamespace(calendar=BaoStockCalendar(dates), listing_intervals=())

        @staticmethod
        def training_row_upper_bound(_dates) -> int:
            return 66

        @staticmethod
        def iter_training_rows(_dates, progress, verification_progress=None):
            for point in rows:
                yield HistoryTrainingRow("600000", point.trade_date, "main", "银行", point)
            progress(66)

    with SQLiteV3TrainingSampleIndex(tmp_path / "samples.sqlite3") as index:
        BuildTrainingSamples(HistoryInput(), ("600000",), TomorrowTrainingWindow(split), index)  # type: ignore[arg-type]
        row = index._connection.run(
            "SELECT trade_date, target_t1, target_t2, target_t3, target_t4, target_t5, target_d25_aggregate "
            "FROM samples WHERE trade_date=?",
            ((dates[60] - date(1970, 1, 1)).days,),
        ).fetchone()

        assert row is not None
        assert row[0] == (dates[60] - date(1970, 1, 1)).days
        assert row[1:] == pytest.approx((0.0,) * 6)
        assert "raw_samples" not in {
            item[0] for item in index._connection.run("SELECT name FROM sqlite_master WHERE type='table'")
        }


def test_training_window_and_sample_dates_preserve_the_frozen_tomorrow_contract() -> None:
    dates = tuple(date(2021, 1, 1) + timedelta(days=index) for index in range(1_600))
    split = BuildTrainingDateSplit(dates, parent_manifest_hash="a" * 64)
    window = TomorrowTrainingWindow(split)

    assert window.readable_dates.isdisjoint(split.point_in_time_holdout_dates)
    with pytest.raises(ValueError, match="point-in-time holdout"):
        window.require_readable((split.point_in_time_holdout_dates[0],))
    sample_day, next_day, indices = AlignedSampleDates(dates[:100], set(dates[:100]), frozenset(dates[:100]))[0]
    assert sample_day == dates[60]
    assert next_day == dates[61]
    assert indices == (60, 59, 57, 55, 40, 20, 0)


def test_training_uses_shared_exposure_and_pre_cost_target_contracts() -> None:
    momenta = ((1.0, 10.0), (3.0, 8.0), (2.0, 6.0), (8.0, 4.0))
    boards = ("main", "main", "star", "star")
    industries = ("bank", "software", "bank", "software")
    amounts = (10.0, 20.0, 15.0, 30.0)
    result = ResidualizeSampleDay(momenta, boards, industries, amounts)
    expected = tuple(
        ResidualizeExposure(
            tuple(row[offset] for row in momenta),
            boards,
            amounts,
            industries=industries,
            contract=TRAINED_HEAD_EXPOSURE_CONTRACT,
        )
        for offset in range(2)
    )

    assert result == expected
    assert TrainingAlphaTarget(next_return=0.06, benchmark_return=0.01) == pytest.approx(0.05)


def test_training_exposure_features_do_not_change_when_peer_future_label_is_missing() -> None:
    day = date(2026, 1, 5)
    samples = tuple(
        _PendingSample(
            f"60000{index}",
            day,
            0,
            ("main", "chinext", "star")[index],
            f"industry-{index}",
            100_000_000.0 + index * 1_000_000.0,
            (0.01, 0.02, 0.03, *(value + index / 10 for value in (0.1, 0.2, 0.3, 0.4, 0.5))),
            10.0,
            [0.01] * 5,
        )
        for index in range(3)
    )
    before = _finalize_sample_day(samples, V2_TRAINING_PROFILE)
    samples[2].future_returns[0] = None
    after = _finalize_sample_day(samples, V2_TRAINING_PROFILE)

    assert len(before) == 3 and len(after) == 3
    assert tuple(item.targets[0] for item in after) == (0.0, 0.0, None)
    assert all(item.targets[-1] is not None for item in after)
    assert before[0].features == after[0].features


@pytest.mark.parametrize("calibration_failure", (False, True))
def test_model_progress_keeps_real_industry_count_for_each_head(monkeypatch, calibration_failure) -> None:
    dates = tuple(date(2021, 1, 1) + timedelta(days=index) for index in range(1_600))
    split = BuildTrainingDateSplit(dates, parent_manifest_hash="a" * 64)
    updates: list[TomorrowTrainingProgress] = []

    class Samples:
        @staticmethod
        def require_split(_split) -> None:
            pass

        @staticmethod
        def share_industry(_industry, *, checkpoint):
            checkpoint()
            return nullcontext()

        @staticmethod
        def industry_counts(_contract) -> tuple[V3TrainingIndustryCounts, ...]:
            assert updates[-1].stage == "industry_statistics"
            assert updates[-1].state == "started"
            if calibration_failure:
                return tuple(V3TrainingIndustryCounts(name, 20_000, 20, 2, 1, 2) for name in ("银行", "软件"))
            return (
                V3TrainingIndustryCounts("银行", 0, 0, 0, 0),
                V3TrainingIndustryCounts("软件", 0, 0, 0, 0),
            )

        @staticmethod
        def split_count(_split_name, _contract) -> int:
            raise AssertionError("industry counts must supply totals without another scan")

    progress = SimpleNamespace(publish=updates.append)

    def reject_calibration(*_args):
        raise CalibrationUnavailableError("calibration_prediction_variance_insufficient")

    monkeypatch.setattr("trader.training.infra.model_fitting._fit_industry", reject_calibration)

    result, = FitIndustryModels(
        Samples(),
        split,
        (TOMORROW_HEAD_CONTRACT,),
        RidgeFittingParameters(20_000, 1.0),
        progress=progress,  # type: ignore[arg-type]
    )

    assert result.models == ()
    assert result.training_rows == result.validation_rows == 0
    assert result.candidate_training_rows == (40_000 if calibration_failure else 0)
    assert [(item.stage, item.state, item.completed_units, item.total_units) for item in updates[:3]] == [
        ("industry_statistics", "started", 0, 1),
        ("industry_statistics", "completed", 1, 1),
        ("model_fit", "started", 0, 2),
    ]
    assert all(item.strategy is Strategy.TOMORROW for item in updates)
    assert all(item.total_units == 2 for item in updates if item.stage == "model_fit")
    assert (updates[-1].state, updates[-1].completed_units) == ("completed", 2)
    skipped = [item.fit_detail for item in updates if item.fit_detail and item.fit_detail.phase == "skipped"]
    assert [item.industry for item in skipped] == ["银行", "软件"]
    assert [item.completed_units for item in updates if item.fit_detail and item.fit_detail.phase == "skipped"] == [
        1,
        2,
    ]
    assert all(item.training_rows == (20_000 if calibration_failure else 0) for item in skipped)
    assert [item.failure_reason for item in skipped] == [item.reason for item in result.failures]
    training = [item.fit_detail for item in updates if item.fit_detail and item.fit_detail.phase == "training"]
    assert [item.industry for item in training] == (["银行", "软件"] if calibration_failure else [])


def test_training_progress_rejects_impossible_counts() -> None:
    progress = TomorrowTrainingProgress("history_conversion", "running", 50, 100, produced_units=40)
    assert progress.produced_units == 40
    with pytest.raises(ValueError, match="counts"):
        TomorrowTrainingProgress("history_conversion", "running", 101, 100)


def _fitted_result(width: int, parameters) -> IndustryFitResult:
    common = ((0.0,) * width, (1.0,) * width, 0.0, 1.0)
    model = (
        TrainedRidgeModelSnapshot(*common, 0.0, (0.1,) * width)
        if isinstance(parameters, RidgeFittingParameters)
        else TrainedLightGBMModelSnapshot(*common, "fixture", 1)
    )
    metrics = PredictionMetrics(1, 1, 1, 1, 1, 0.0, 0.0, None, 0)
    return IndustryFitResult(
        (FittedIndustryModel("银行", model, 1, CalibrationResult(2, 2, 1e-4, 0.0, 1.0)),),
        (),
        metrics,
        (("银行", metrics),),
        1,
        0,
    )


def _training_point(day: date, close: float) -> HistoryTrainingPoint:
    return HistoryTrainingPoint(day, close, 1_000.0)

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from trader.download.domain.baostock_daily import (
    BaoStockCalendar,
    build_baostock_training_split,
)
from trader.download.domain.history_revision import HistoryTrainingPoint, HistoryTrainingRow
from trader.download.infra.history_archive_repack import HistoryArchiveRepackFenceError
from trader.download.infra.history_control_index import HistoryMaintenanceAlreadyRunningError
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.residualization import TRAINED_HEAD_EXPOSURE_CONTRACT, residualize_exposure
from trader.training.application.tomorrow_training import TomorrowTrainingProgress, TomorrowTrainingWindow
from trader.training.domain.tomorrow_training_input import REQUIRED_DAILY_FIELDS, FrozenDailyInputDescriptor
from trader.training.infra.engine import (
    _cleanup_abandoned_workspaces,
    _mature_label_cutoff,
    _training_contract_hash,
)
from trader.training.infra.history.history_training_input import HistoryTrainingInputError, HistoryTrainingInputSnapshot
from trader.training.infra.model_fitting import RidgeFittingParameters, fit_industry_models
from trader.training.infra.profile.v2.training import run_v2_training as _run_v2_training
from trader.training.infra.profile.v3.contracts import (
    D25_HEAD_CONTRACT,
    HEAD_CONTRACTS,
    TOMORROW_HEAD_CONTRACT,
    V3_TRAINING_PROFILE,
)
from trader.training.infra.profile.v3.sample_builder import (
    aligned_sample_dates,
    build_training_samples,
    residualize_sample_day,
    training_alpha_target,
)
from trader.training.infra.profile.v3.training import (
    run_tomorrow_training as _run_tomorrow_training,
)
from trader.training.infra.profile.v3.training import run_v3_training as _run_v3_training
from trader.training.infra.profile.v3.training_sample_repository import (
    SQLiteV3TrainingSampleRepository,
    V3TrainingIndustryCounts,
    V3TrainingSample,
)
from trader.training.infra.sample_builder import TrainingSampleBuildRequest
from trader.training.infra.training_settings import load_profile_training_settings

PROJECT_ROOT = Path(__file__).resolve().parents[6]


def run_v2_training(history_root: Path, train_root: Path, **kwargs):
    return _run_v2_training(
        history_root,
        train_root,
        settings=load_profile_training_settings(PROJECT_ROOT, "v2"),
        **kwargs,
    )


def run_v3_training(history_root: Path, train_root: Path, **kwargs):
    return _run_v3_training(
        history_root,
        train_root,
        settings=load_profile_training_settings(PROJECT_ROOT, "v3"),
        **kwargs,
    )


def run_tomorrow_training(history_root: Path, train_root: Path, **kwargs):
    return _run_tomorrow_training(
        history_root,
        train_root,
        settings=load_profile_training_settings(PROJECT_ROOT, "v3"),
        **kwargs,
    )


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
        has_industry_window=lambda _dates, _sessions, **_kwargs: True,
        industry_sample_upper_bounds=lambda *_args: (("fixture", 100_000),),
    )


def _patch_archive(monkeypatch: pytest.MonkeyPatch, archive: SimpleNamespace) -> None:
    monkeypatch.setattr(
        "trader.training.infra.engine.SQLiteHistoryTrainingInputArchive.open",
        lambda _path: archive,
    )


def test_manual_training_does_not_compare_revisions_or_cadence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive = _cadence_archive(tmp_path / "history")
    _patch_archive(monkeypatch, archive)
    monkeypatch.setattr(
        "trader.training.infra.history.history_training_due.evaluate_history_training_due",
        lambda *_args, **_kwargs: pytest.fail("manual training must not evaluate cadence or revisions"),
    )
    monkeypatch.setattr(
        "trader.training.infra.engine.build_training_samples",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("scan reached")),
    )

    result = run_tomorrow_training(tmp_path / "history", tmp_path / "train")

    assert result.status == "blocked"
    assert result.failure_reasons == ("profile_training_failed",)
    assert result.run_id is not None


def test_training_rejects_empty_samples_instead_of_reusing_existing_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _cadence_archive(tmp_path / "history")
    _patch_archive(monkeypatch, archive)
    monkeypatch.setattr(
        "trader.training.infra.engine.build_training_samples",
        lambda *_args, **_kwargs: None,
    )

    result = run_tomorrow_training(tmp_path / "history", tmp_path / "train")

    assert result.status == "blocked"
    assert result.failure_reasons == ("profile_training_rows_empty",)
    assert result.model_hash == result.report_hash == ""


@pytest.mark.parametrize("run_training", (run_v2_training, run_v3_training))
def test_sample_sqlite_failure_returns_both_heads_and_releases_locks(tmp_path, monkeypatch, run_training):
    archive = _cadence_archive(tmp_path / "history")
    _patch_archive(monkeypatch, archive)

    def fail_samples(_request):
        raise sqlite3.OperationalError("fixture disk full with private supplier details")

    monkeypatch.setattr("trader.training.infra.engine.build_training_samples", fail_samples)
    result = run_training(tmp_path / "history", tmp_path / "train")
    assert result.status == "blocked"
    assert len(result.heads) == 2
    assert all(head.failure_reasons == ("training_database_failed",) for head in result.heads)
    assert all(not head.model_hash and not head.report_hash for head in result.heads)
    assert result.sample_database_peak_bytes > 0
    assert not tuple((tmp_path / "train").glob("*/.sample-cache/complete.json"))
    monkeypatch.setattr("trader.training.infra.engine.build_training_samples", lambda _request: None)
    retry = run_training(tmp_path / "history", tmp_path / "train")
    assert all(head.failure_reasons == ("profile_training_rows_empty",) for head in retry.heads)


def test_missing_industry_in_fit_split_blocks_before_price_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    archive = _cadence_archive(tmp_path / "history")
    archive.has_industry_window = lambda _dates, _sessions, **kwargs: kwargs.get("anchor_dates") is None
    archive.verify_partitions = lambda *_args: pytest.fail("preflight must run before expensive price validation")
    _patch_archive(monkeypatch, archive)
    result = run_tomorrow_training(tmp_path / "history", tmp_path / "train")
    assert result.status == "blocked"
    assert result.failure_reasons == ("history_industry_split_coverage_insufficient",)


@pytest.mark.parametrize("run_training,profile", ((run_v2_training, "v2"), (run_v3_training, "v3")))
@pytest.mark.parametrize("failure", (ValueError, sqlite3.OperationalError))
def test_fitting_failure_reuses_only_complete_samples_on_retry(tmp_path, monkeypatch, run_training, profile, failure):
    archive = _cadence_archive(tmp_path / "history")
    _patch_archive(monkeypatch, archive)
    builds = 0

    def build_samples(request):
        nonlocal builds
        builds += 1
        request.repository.add_final(
            (
                V3TrainingSample(
                    "600000",
                    request.window.split.model_fit_dates[0],
                    "main",
                    "银行",
                    1.0,
                    (0.0,) * len(request.profile.heads[0].feature_positions),
                    (0.01, 0.02, 0.03, 0.04, 0.05, 0.035),
                ),
            )
        )
        request.repository.prepare_for_model_fitting(request.window.split)

    def fail(*_args, **_kwargs):
        raise failure("fixture_fit_failed")

    monkeypatch.setattr("trader.training.infra.engine.build_training_samples", build_samples)
    monkeypatch.setattr("trader.training.infra.engine.fit_industry_models", fail)
    first = run_training(tmp_path / "history", tmp_path / "train")
    assert first.status == "blocked"
    assert first.sample_database_peak_bytes > 0
    assert (tmp_path / "train" / profile / ".sample-cache/complete.json").is_file()
    if failure is sqlite3.OperationalError:
        assert all(head.failure_reasons == ("training_database_failed",) for head in first.heads)

    def reject_changed_input(_progress):
        raise HistoryTrainingInputError("history_training_data_incomplete")

    archive.verify_partitions = reject_changed_input
    monkeypatch.setattr(
        "trader.training.infra.engine.fit_industry_models",
        lambda *_args, **_kwargs: pytest.fail("cached samples cannot bypass input verification"),
    )
    invalid = run_training(tmp_path / "history", tmp_path / "train")
    assert all(head.failure_reasons == ("history_training_data_incomplete",) for head in invalid.heads)
    assert builds == 1

    def fit(_samples, _split, contract, algorithm, *_args, **_kwargs):
        return ({"银行": _industry_model(len(contract.feature_positions), algorithm)}, 1, 1)

    monkeypatch.setattr("trader.training.infra.engine.fit_industry_models", fit)
    monkeypatch.setattr("trader.training.infra.engine.publish_head_bundle", lambda *_args: None)
    verified = []
    archive.verify_partitions = lambda _progress: verified.append(True)
    second = run_training(tmp_path / "history", tmp_path / "train")
    assert second.status == "engineering_ready" and builds == 1
    assert verified == [True]
    assert not (tmp_path / "train" / profile / ".sample-cache/samples.sqlite3").exists()


def test_provably_insufficient_industry_rows_fail_before_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive = _cadence_archive(tmp_path / "history")
    archive.industry_sample_upper_bounds = lambda *_args: (("银行", 19999),)
    _patch_archive(monkeypatch, archive)
    monkeypatch.setattr(
        "trader.training.infra.engine.build_training_samples", lambda *_args: pytest.fail("must fail early")
    )
    result = run_tomorrow_training(tmp_path / "history", tmp_path / "train")
    assert result.status == "blocked"
    assert result.failure_reasons == ("history_industry_training_rows_insufficient",)


def test_training_rejects_insufficient_industry_history_before_partition_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _cadence_archive(tmp_path / "history")
    archive.has_industry_window = lambda _dates, _sessions: False
    archive.verify_partitions = lambda _progress=None: pytest.fail("industry preflight must precede partition hashes")
    _patch_archive(monkeypatch, archive)
    monkeypatch.setattr(
        "trader.training.infra.engine.build_training_samples",
        lambda *_args, **_kwargs: pytest.fail("industry preflight must precede the history scan"),
    )

    result = run_tomorrow_training(tmp_path / "history", tmp_path / "train")

    assert result.status == "blocked"
    assert result.failure_reasons == ("history_industry_coverage_insufficient",)
    assert result.model_hash == result.report_hash == ""


def test_v3_training_validates_and_scans_history_once_then_fits_heads_sequentially(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _cadence_archive(tmp_path / "history")
    verify_calls = 0

    def verify(progress=None) -> None:
        nonlocal verify_calls
        verify_calls += 1
        if progress is not None:
            progress(2, 2, 1, 100, 100, "row_count")

    archive.verify_partitions = verify
    _patch_archive(monkeypatch, archive)
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

    def fit(_samples, _split, contract, algorithm, *_args, progress):
        del progress
        fitted.append(contract.strategy)
        width = len(contract.feature_positions)
        return ({"银行": _industry_model(width, algorithm)}, 1, 1)

    monkeypatch.setattr("trader.training.infra.engine.fit_industry_models", fit)
    published: list[tuple[Strategy, Path]] = []

    def publish(staging, output, strategy, _profile, _identity) -> None:
        assert {path.name for path in staging.iterdir()} == {"model.json", "report.json", "training-input.json"}
        published.append((strategy, output))

    monkeypatch.setattr("trader.training.infra.engine.publish_head_bundle", publish)

    result = run_v3_training(tmp_path / "history", tmp_path / "train")

    assert result.status == "engineering_ready"
    # The real streaming reader now verifies each month immediately before
    # consumption. This synthetic builder must not trigger an eager full scan.
    assert verify_calls == 0 and scan_calls == 1
    assert fitted == [Strategy.TOMORROW, Strategy.D25]
    assert published == [
        (Strategy.TOMORROW, tmp_path / "train" / "v3" / "tomorrow"),
        (Strategy.D25, tmp_path / "train" / "v3" / "d25"),
    ]
    assert all(head.status == "engineering_ready" for head in result.heads)
    assert all(head.label_cutoff is not None for head in result.heads)

    repeated = run_v3_training(tmp_path / "history", tmp_path / "train")

    assert repeated.status == "engineering_ready"
    assert verify_calls == 0 and scan_calls == 2
    assert fitted == [Strategy.TOMORROW, Strategy.D25] * 2
    assert published[2:] == published[:2]
    assert repeated.heads == result.heads


def test_v3_head_maturity_and_contract_hashes_are_independent_and_stable(tmp_path: Path) -> None:
    snapshot = _cadence_archive(tmp_path / "history").snapshot

    assert _mature_label_cutoff(snapshot, TOMORROW_HEAD_CONTRACT) == snapshot.calendar.open_dates[-2]
    assert _mature_label_cutoff(snapshot, D25_HEAD_CONTRACT) == snapshot.calendar.open_dates[-6]
    settings = load_profile_training_settings(PROJECT_ROOT, "v3")
    hashes = {_training_contract_hash(V3_TRAINING_PROFILE, contract, settings) for contract in HEAD_CONTRACTS}
    assert len(hashes) == 2
    assert _training_contract_hash(V3_TRAINING_PROFILE, TOMORROW_HEAD_CONTRACT, settings) == _training_contract_hash(
        V3_TRAINING_PROFILE, TOMORROW_HEAD_CONTRACT, settings
    )


def test_v3_training_always_fits_both_heads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive = _cadence_archive(tmp_path / "history")
    _patch_archive(monkeypatch, archive)

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

    def fit(_samples, _split, contract, algorithm, *_args, progress):
        del progress
        fitted.append(contract.strategy)
        return ({"银行": _industry_model(len(contract.feature_positions), algorithm)}, 1, 1)

    monkeypatch.setattr("trader.training.infra.engine.fit_industry_models", fit)
    monkeypatch.setattr("trader.training.infra.engine.publish_head_bundle", lambda *_args, **_kwargs: None)

    result = run_v3_training(tmp_path / "history", tmp_path / "train")

    assert fitted == [Strategy.TOMORROW, Strategy.D25]
    assert [head.status for head in result.heads] == ["engineering_ready", "engineering_ready"]


def test_training_reports_both_heads_when_the_shared_scan_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _cadence_archive(tmp_path / "history")
    _patch_archive(monkeypatch, archive)
    monkeypatch.setattr(
        "trader.training.infra.engine.build_training_samples",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("forced failure")),
    )

    result = run_v3_training(tmp_path / "history", tmp_path / "train")

    assert result.status == "blocked"
    assert all(head.status == "blocked" for head in result.heads)
    assert all(head.failure_reasons == ("profile_training_failed",) for head in result.heads)
    assert [head.label_cutoff for head in result.heads] == [
        archive.snapshot.calendar.open_dates[-2],
        archive.snapshot.calendar.open_dates[-6],
    ]


def test_training_does_not_open_a_second_snapshot_while_history_maintenance_is_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _cadence_archive(tmp_path / "history")
    open_calls: list[Path] = []

    def open_archive(path: Path) -> SimpleNamespace:
        open_calls.append(path)
        return archive

    class BusyMaintenanceLock:
        def __init__(self, path: Path) -> None:
            self._path = path

        def __enter__(self) -> None:
            if self._path.name == ".maintenance.lock":
                raise HistoryMaintenanceAlreadyRunningError("busy")

        def __exit__(self, *_args: object) -> None:
            pass

    monkeypatch.setattr("trader.training.infra.engine.SQLiteHistoryTrainingInputArchive.open", open_archive)
    monkeypatch.setattr("trader.training.infra.engine.HistoryMaintenanceLock", BusyMaintenanceLock)

    result = run_v3_training(tmp_path / "history", tmp_path / "train")

    assert all(head.failure_reasons == ("history_maintenance_running",) for head in result.heads)
    assert open_calls == [tmp_path / "history"]


def test_training_respects_the_history_repack_fence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive = _cadence_archive(tmp_path / "history")
    _patch_archive(monkeypatch, archive)
    monkeypatch.setattr(
        "trader.training.infra.engine.require_history_archive_repack_inactive",
        lambda _root: (_ for _ in ()).throw(HistoryArchiveRepackFenceError("fenced")),
    )

    result = run_v3_training(tmp_path / "history", tmp_path / "train")

    assert all(head.failure_reasons == ("history_archive_repack_activation_pending",) for head in result.heads)


def test_training_cleanup_removes_only_owned_abandoned_workspaces(tmp_path: Path) -> None:
    abandoned = tmp_path / ".training-sample-workspace.abandoned"
    staging = tmp_path / "d25" / ".bundle-staging.abandoned"
    preserved = tmp_path / "tomorrow" / "model.json"
    abandoned.mkdir()
    staging.mkdir(parents=True)
    preserved.parent.mkdir(parents=True)
    preserved.touch()

    _cleanup_abandoned_workspaces(tmp_path, HEAD_CONTRACTS)

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
        def iter_training_rows(_dates, progress, verification_progress=None):
            for point in rows:
                yield HistoryTrainingRow("600000", point.trade_date, "main", "银行", point)
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
    dates = tuple(date(2021, 1, 1) + timedelta(days=index) for index in range(1_250))
    split = build_baostock_training_split(dates, parent_manifest_hash="a" * 64)
    updates: list[TomorrowTrainingProgress] = []

    class Samples:
        @staticmethod
        def require_split(_split) -> None:
            pass

        @staticmethod
        def industry_counts(_contract) -> tuple[V3TrainingIndustryCounts, ...]:
            assert updates[-1].stage == "industry_statistics"
            assert updates[-1].state == "started"
            return (
                V3TrainingIndustryCounts("银行", 0, 0, 0, 0),
                V3TrainingIndustryCounts("软件", 0, 0, 0, 0),
            )

        @staticmethod
        def split_count(_split_name, _contract) -> int:
            raise AssertionError("industry counts must supply totals without another scan")

    progress = SimpleNamespace(publish=updates.append)

    models, training_rows, validation_rows = fit_industry_models(
        Samples(),
        split,
        TOMORROW_HEAD_CONTRACT,
        RidgeFittingParameters(20_000, 1.0),
        progress=progress,  # type: ignore[arg-type]
    )

    assert models == {}
    assert training_rows == validation_rows == 0
    assert [(item.stage, item.state, item.completed_units, item.total_units) for item in updates] == [
        ("industry_statistics", "started", 0, 1),
        ("industry_statistics", "completed", 1, 1),
        ("model_fit", "started", 0, 2),
        ("model_fit", "running", 1, 2),
        ("model_fit", "completed", 2, 2),
    ]
    assert all(item.strategy is Strategy.TOMORROW for item in updates)


def test_training_progress_rejects_impossible_counts() -> None:
    progress = TomorrowTrainingProgress("history_conversion", "running", 50, 100, produced_units=40)
    assert progress.produced_units == 40
    with pytest.raises(ValueError, match="counts"):
        TomorrowTrainingProgress("history_conversion", "running", 101, 100)


def _industry_model(width: int, algorithm: str) -> dict[str, object]:
    model = {
        "transformer_means": [0.0] * width,
        "transformer_scales": [1.0] * width,
        "calibration_intercept": 0.0,
        "calibration_slope": 1.0,
        "training_rows": 1,
        "validation_rows": 1,
    }
    if algorithm == "lightgbm":
        model.update({"lightgbm_model": "fixture", "lightgbm_best_iteration": 1})
    else:
        model.update({"ridge_intercept": 0.0, "ridge_coefficients": [0.1] * width})
    return model


def _training_point(day: date, close: float) -> HistoryTrainingPoint:
    return HistoryTrainingPoint(day, close, 1_000.0)

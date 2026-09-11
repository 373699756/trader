import json
from datetime import date, datetime, timedelta
from importlib import resources
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from trader.application.research.tomorrow_training import TomorrowTrainingProgress, TomorrowTrainingWindow
from trader.domain.recommendation.model_scoring import V3_EXPOSURE_CONTRACT, residualize_exposure
from trader.domain.research.baostock_daily import BaoStockCalendar, build_baostock_training_split
from trader.domain.research.history_control import HistoryTrainingDueState
from trader.domain.research.tomorrow_training_input import REQUIRED_DAILY_FIELDS, FrozenDailyInputDescriptor
from trader.infra.research.history_control_repository import HistoryMaintenanceAlreadyRunningError
from trader.infra.research.history_training_input import HistoryTrainingInputSnapshot
from trader.infra.scoring.artifact_hashing import artifact_content_hash
from trader.infra.scoring.profiles.v3.bundle_codec import decode_tomorrow_bundle
from trader.infra.scoring.profiles.v3.sample_store import V3StoredSample
from trader.infra.scoring.profiles.v3.training import (
    _aligned_sample_dates,
    _model_document,
    _residualize_sample_day,
    _training_output_directory,
    _TrainingArtifactContext,
    run_tomorrow_training,
    training_alpha_target,
)


def _cadence_archive(archive_root: Path) -> SimpleNamespace:
    dates = tuple(date(2020, 1, 1) + timedelta(days=index) for index in range(2000))
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
        requested_sessions=2000,
        primary_key=("code", "trade_date"),
        fields=REQUIRED_DAILY_FIELDS,
        raw_qfq_layout="same_row",
        row_hash_algorithm="sha256",
        frozen=True,
    )
    return SimpleNamespace(
        snapshot=snapshot,
        archive_root=archive_root,
        describe_frozen_daily_input=lambda: descriptor,
    )


def _active_bundle(archive: SimpleNamespace, trained_position: int, *, current: bool = False) -> SimpleNamespace:
    dates = archive.snapshot.calendar.open_dates
    return SimpleNamespace(
        training_input_hash=archive.snapshot.active_snapshot_hash if current else "b" * 64,
        label_cutoff=dates[trained_position],
        content_hash="c" * 64,
        report_hash="d" * 64,
        source_identity_hash=archive.snapshot.source_identity_hash,
        industries=(("银行", object()),),
        training_rows=100,
        validation_rows=20,
    )


def _due(
    archive: SimpleNamespace,
    trained_position: int | None,
    reason: str,
    *,
    current: bool = False,
) -> SimpleNamespace:
    dates = archive.snapshot.calendar.open_dates
    current_cutoff = dates[-2]
    baseline = dates[trained_position] if trained_position is not None else None
    state = HistoryTrainingDueState(
        "due-test",
        reason,  # type: ignore[arg-type]
        baseline,
        current_cutoff,
        sum(baseline < day <= current_cutoff for day in dates) if baseline is not None else 0,
        reason == "input_revision_due",
        datetime(2026, 9, 11, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    bundle = _active_bundle(archive, trained_position, current=current) if trained_position is not None else None
    return SimpleNamespace(state=state, bundle=bundle, invalidated_cache_dates=())


def test_training_cadence_stops_before_model_work_on_the_nineteenth_matured_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _cadence_archive(tmp_path / "history" / "baostock")
    current_label_position = len(archive.snapshot.calendar.open_dates) - 2
    monkeypatch.setattr(
        "trader.infra.scoring.profiles.v3.training.SQLiteHistoryTrainingInputArchive.open",
        lambda _path: archive,
    )
    monkeypatch.setattr(
        "trader.infra.scoring.profiles.v3.training.evaluate_history_training_due",
        lambda *_args: _due(archive, current_label_position - 19, "not_due"),
    )
    monkeypatch.setattr(
        "trader.infra.scoring.profiles.v3.training._build_split",
        lambda *_args: pytest.fail("model work must not start before cadence is due"),
    )

    result = run_tomorrow_training(tmp_path / "history", tmp_path / "train", source_commit="e" * 40)

    assert result.status == "not_due"
    assert result.matured_label_days_since_training == 19
    assert result.training_due is False
    assert result.training_due_reason == "not_due"


def test_training_failure_on_the_twentieth_day_keeps_the_successful_bundle_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _cadence_archive(tmp_path / "history" / "baostock")
    current_label_position = len(archive.snapshot.calendar.open_dates) - 2
    monkeypatch.setattr(
        "trader.infra.scoring.profiles.v3.training.SQLiteHistoryTrainingInputArchive.open",
        lambda _path: archive,
    )
    monkeypatch.setattr(
        "trader.infra.scoring.profiles.v3.training.evaluate_history_training_due",
        lambda *_args: _due(archive, current_label_position - 20, "cadence_due"),
    )
    monkeypatch.setattr(
        "trader.infra.scoring.profiles.v3.training._build_split",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("forced failure")),
    )

    first = run_tomorrow_training(tmp_path / "history", tmp_path / "train", source_commit="e" * 40)
    second = run_tomorrow_training(tmp_path / "history", tmp_path / "train", source_commit="e" * 40)

    assert first.status == second.status == "blocked"
    assert first.training_due_reason == second.training_due_reason == "cadence_due"
    assert first.matured_label_days_since_training == second.matured_label_days_since_training == 20
    assert first.training_due is second.training_due is True


def test_training_returns_already_current_only_for_the_same_successful_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _cadence_archive(tmp_path / "history" / "baostock")
    current_label_position = len(archive.snapshot.calendar.open_dates) - 2
    monkeypatch.setattr(
        "trader.infra.scoring.profiles.v3.training.SQLiteHistoryTrainingInputArchive.open",
        lambda _path: archive,
    )
    monkeypatch.setattr(
        "trader.infra.scoring.profiles.v3.training.evaluate_history_training_due",
        lambda *_args: _due(archive, current_label_position, "not_due", current=True),
    )

    result = run_tomorrow_training(tmp_path / "history", tmp_path / "train", source_commit="e" * 40)

    assert result.status == "already_current"
    assert result.label_cutoff == archive.snapshot.calendar.open_dates[current_label_position]
    assert result.matured_label_days_since_training == 0


def test_successful_bundle_publication_is_the_only_event_that_clears_due_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _cadence_archive(tmp_path / "history" / "baostock")
    sample_day = archive.snapshot.calendar.open_dates[100]
    monkeypatch.setattr(
        "trader.infra.scoring.profiles.v3.training.SQLiteHistoryTrainingInputArchive.open",
        lambda _path: archive,
    )
    monkeypatch.setattr(
        "trader.infra.scoring.profiles.v3.training.evaluate_history_training_due",
        lambda *_args: _due(archive, None, "initial_training_required"),
    )

    def build_samples(_archive, _codes, _window, store, *, progress) -> None:
        del progress
        store.add_final((V3StoredSample("600000", sample_day, "main", "银行", 1.0, (0.0,) * 6, 0.01),))

    monkeypatch.setattr("trader.infra.scoring.profiles.v3.training._build_samples", build_samples)
    monkeypatch.setattr(
        "trader.infra.scoring.profiles.v3.training._fit_models",
        lambda _samples, _split: ({"银行": {}}, 1, 1),
    )
    published: list[str] = []
    monkeypatch.setattr(
        "trader.infra.scoring.profiles.v3.training.publish_tomorrow_bundle",
        lambda *_args, **_kwargs: published.append("published"),
    )

    result = run_tomorrow_training(tmp_path / "history", tmp_path / "train", source_commit="e" * 40)

    assert published == ["published"]
    assert result.status == "engineering_ready"
    assert result.training_due is False
    assert result.training_due_reason == "not_due"
    assert result.matured_label_days_since_training == 0


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

    monkeypatch.setattr(
        "trader.infra.scoring.profiles.v3.training.SQLiteHistoryTrainingInputArchive.open",
        open_archive,
    )
    monkeypatch.setattr(
        "trader.infra.scoring.profiles.v3.training.HistoryMaintenanceLock",
        BusyMaintenanceLock,
    )
    monkeypatch.setattr(
        "trader.infra.scoring.profiles.v3.training.evaluate_history_training_due",
        lambda *_args: pytest.fail("training must not inspect due state without the maintenance lock"),
    )

    result = run_tomorrow_training(tmp_path / "history", tmp_path / "train", source_commit="e" * 40)

    assert result.status == "blocked"
    assert result.failure_reasons == ("history_maintenance_running",)
    assert result.training_input_hash == archive.snapshot.active_snapshot_hash
    assert open_calls == [tmp_path / "history"]


def test_v3_training_outputs_json_directly_under_the_profile_directory(tmp_path: Path) -> None:
    output = _training_output_directory(tmp_path)

    assert output == tmp_path / "tomorrow-v3"
    assert output.parent == tmp_path


def test_training_window_never_authorizes_the_latest_two_hundred_dates() -> None:
    dates = tuple(date(2021, 1, 1) + timedelta(days=index) for index in range(1250))
    split = build_baostock_training_split(dates, parent_manifest_hash="a" * 64)
    window = TomorrowTrainingWindow(split)

    assert window.readable_dates.isdisjoint(split.point_in_time_holdout_dates)
    assert split.daily_proxy_holdout_dates[-1] in window.readable_dates
    with pytest.raises(ValueError, match="point-in-time holdout"):
        window.require_readable((split.point_in_time_holdout_dates[0],))


def test_training_progress_rejects_impossible_counts() -> None:
    assert TomorrowTrainingProgress("sample_build", 50, 100).processed_codes == 50
    with pytest.raises(ValueError, match="counts"):
        TomorrowTrainingProgress("sample_build", 101, 100)


def test_training_window_rejects_dates_outside_the_frozen_manifest() -> None:
    dates = tuple(date(2021, 1, 1) + timedelta(days=index) for index in range(1250))
    window = TomorrowTrainingWindow(build_baostock_training_split(dates, parent_manifest_hash="a" * 64))

    with pytest.raises(ValueError, match="outside the frozen split"):
        window.require_readable((date(2020, 1, 1),))


def test_v3_sample_dates_use_global_calendar_and_reject_gaps() -> None:
    dates = tuple(date(2021, 1, 1) + timedelta(days=index) for index in range(100))
    readable = frozenset(dates)
    available = set(dates)
    available.remove(dates[69])

    samples = _aligned_sample_dates(dates, available, readable)

    assert all(day != dates[68] and day != dates[69] and next_day != dates[69] for day, next_day, _ in samples)
    assert all(next_day == dates[indices[0] + 1] for _day, next_day, indices in samples)


def test_v3_sample_dates_do_not_pad_short_listing_history() -> None:
    dates = tuple(date(2021, 1, 1) + timedelta(days=index) for index in range(100))
    available = set(dates[30:])

    samples = _aligned_sample_dates(dates, available, frozenset(dates))

    assert samples
    assert samples[0][0] == dates[90]


def test_v3_sample_dates_use_the_same_skip_five_lookbacks_as_online_features() -> None:
    dates = tuple(date(2021, 1, 1) + timedelta(days=index) for index in range(100))

    samples = _aligned_sample_dates(dates, set(dates), frozenset(dates))

    day, next_day, indices = samples[0]
    assert day == dates[60]
    assert next_day == dates[61]
    assert indices == (60, 59, 57, 55, 40, 20, 0)


def test_v3_sample_dates_require_every_amount_window_session() -> None:
    dates = tuple(date(2021, 1, 1) + timedelta(days=index) for index in range(100))
    available = set(dates)
    available.remove(dates[70])

    samples = _aligned_sample_dates(dates, available, frozenset(dates))

    assert all(day != dates[80] for day, _next_day, _indices in samples)


def test_v3_training_uses_the_shared_online_exposure_contract() -> None:
    momenta = ((1.0, 10.0), (3.0, 8.0), (2.0, 6.0), (8.0, 4.0))
    boards = ("main", "main", "star", "star")
    industries = ("bank", "software", "bank", "software")
    amounts = (10.0, 20.0, 15.0, 30.0)

    result = _residualize_sample_day(momenta, boards, industries, amounts)
    expected = tuple(
        residualize_exposure(
            tuple(row[offset] for row in momenta),
            boards,
            amounts,
            industries=industries,
            contract=V3_EXPOSURE_CONTRACT,
        )
        for offset in range(2)
    )

    for actual_values, expected_values in zip(result, expected, strict=True):
        assert actual_values == pytest.approx(expected_values)


def test_v3_training_target_keeps_round_trip_cost_out_of_the_alpha_label() -> None:
    target = training_alpha_target(next_return=0.06, benchmark_return=0.01)

    assert target == pytest.approx(0.05)
    assert target - 0.002 == pytest.approx(0.048)


def test_v3_training_document_is_accepted_by_the_production_codec() -> None:
    p2 = json.loads(
        resources.files("trader.infra.scoring.profiles.v2").joinpath("model.json").read_text(encoding="utf-8")
    )
    dates = tuple(date(2021, 1, 1) + timedelta(days=index) for index in range(1250))
    split = build_baostock_training_split(dates, parent_manifest_hash="a" * 64)
    industry_model: dict[str, object] = {
        "transformer_means": [0.0] * 6,
        "transformer_scales": [1.0] * 6,
        "ridge_intercept": 0.0,
        "ridge_coefficients": [0.1] * 6,
        "lightgbm_model": p2["lightgbm_model"],
        "lightgbm_best_iteration": p2["lightgbm_best_iteration"],
        "calibration_intercept": 0.0,
        "calibration_slope": 1.0,
        "training_rows": 20_000,
        "validation_rows": 1_000,
    }
    context = _TrainingArtifactContext(
        "complete_manifest",
        "a" * 64,
        date(2026, 9, 8),
        "1" * 64,
        "3" * 64,
        100,
        100,
        split,
        "d" * 40,
        "e" * 64,
        20_000,
        1_000,
    )
    document = _model_document(
        context,
        "b" * 64,
        {"银行": industry_model},
    )
    document["content_hash"] = artifact_content_hash(document)

    artifact = decode_tomorrow_bundle(document)

    assert artifact.feature_ids[-1] == "qfq_residual_momentum_60d_skip5"
    assert artifact.exposure_contract == V3_EXPOSURE_CONTRACT
    assert artifact.training_input_scope == "complete_manifest"
    assert artifact.training_input_hash == "a" * 64
    assert artifact.training_contract_hash == "e" * 64
    assert artifact.source_commit == "d" * 40
    assert artifact.training_anchor == "15:00_close_proxy"
    assert artifact.historical_status == "historical_data_insufficient"
    assert tuple(industry for industry, _model in artifact.industries) == ("银行",)

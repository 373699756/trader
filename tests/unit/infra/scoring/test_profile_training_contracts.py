from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from trader.download.domain.baostock_daily import BaoStockCalendar, BuildBaostockTrainingSplit
from trader.download.domain.history_revision import HistoryTrainingPoint, HistoryTrainingRow
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.infra.scoring.cost_settings import (
    LoadEvaluationCostScenarios,
    LoadExecutionCostPolicy,
)
from trader.training.application.tomorrow_training import TomorrowTrainingWindow
from trader.training.infra.engine import ProfileTrainingRequest
from trader.training.infra.model_fitting import LightGBMFittingParameters, RidgeFittingParameters
from trader.training.infra.profile.v2.contracts import V2_TRAINING_PROFILE
from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE
from trader.training.infra.research_settings import LoadResearchSettings
from trader.training.infra.sample_builder import (
    BuildJointTrainingSamples,
    BuildTrainingSamples,
    TrainingSampleBuildRequest,
)
from trader.training.infra.sample_index import SQLiteTrainingSampleIndex
from trader.training.infra.training_settings import LoadProfileTrainingSettings

PROJECT_ROOT = Path(__file__).resolve().parents[4]


def test_v2_and_v3_share_engine_contract_shape_but_own_their_differences() -> None:
    assert V2_TRAINING_PROFILE.profile_id == "v2"
    assert V2_TRAINING_PROFILE.output_directory == "v2"
    assert V2_TRAINING_PROFILE.history_sessions == 251
    assert V3_TRAINING_PROFILE.profile_id == "v3"
    assert V3_TRAINING_PROFILE.output_directory == "v3"
    assert V3_TRAINING_PROFILE.history_sessions == 61

    assert tuple(head.strategy for head in V2_TRAINING_PROFILE.heads) == (
        Strategy.TOMORROW,
        Strategy.D25,
    )
    assert tuple(head.strategy for head in V3_TRAINING_PROFILE.heads) == (
        Strategy.TOMORROW,
        Strategy.D25,
    )
    assert "qfq_residual_momentum_120d_skip5" in V2_TRAINING_PROFILE.heads[0].feature_manifest.names
    assert "qfq_residual_momentum_250d_skip5" in V2_TRAINING_PROFILE.heads[0].feature_manifest.names
    assert "market_qfq_momentum_120d_skip5" in V2_TRAINING_PROFILE.heads[0].feature_manifest.names
    assert "market_qfq_momentum_250d_skip5" in V2_TRAINING_PROFILE.heads[0].feature_manifest.names
    assert "qfq_residual_momentum_120d_skip5" not in V3_TRAINING_PROFILE.heads[0].feature_manifest.names


def test_profile_windows_algorithms_costs_and_research_arms_have_typed_configuration_owners() -> None:
    v2 = LoadProfileTrainingSettings(PROJECT_ROOT, "v2")
    v3 = LoadProfileTrainingSettings(PROJECT_ROOT, "v3")
    cost = LoadExecutionCostPolicy(PROJECT_ROOT / "config")
    scenarios = LoadEvaluationCostScenarios(PROJECT_ROOT / "config")
    research = LoadResearchSettings(PROJECT_ROOT / "config")

    assert (v2.fit_dates, v2.history_sessions, v2.momentum_horizons) == (1000, 251, (20, 40, 60, 120, 250))
    assert (v3.fit_dates, v3.history_sessions, v3.momentum_horizons) == (750, 61, (20, 40, 60))
    assert isinstance(v2.fitting, LightGBMFittingParameters)
    assert isinstance(v3.fitting, RidgeFittingParameters)
    assert V2_TRAINING_PROFILE.algorithm == "lightgbm"
    assert V3_TRAINING_PROFILE.algorithm == "ridge"
    assert cost.estimate(0.0) == pytest.approx(0.002)
    assert cost.estimate(1.0) == pytest.approx(0.004)
    assert tuple((item.scenario_id, item.round_trip_return) for item in scenarios) == (
        ("cost_20bp", 0.002),
        ("cost_50bp", 0.005),
        ("cost_100bp", 0.01),
    )
    assert research.deepseek_comparison_arms == ("local_only", "structured_facts_veto")
    assert research.tomorrow_embargo_sessions >= 5
    assert research.ablation_arms == ("rule_only", "rule_model_gate", "industry_model_utility", "pooled_model_utility")


def test_profile_training_modules_do_not_import_each_other() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[4] / "src" / "trader" / "infra" / "scoring" / "profiles"
    v2 = "\n".join(path.read_text(encoding="utf-8") for path in (root / "v2").glob("*.py"))
    v3 = "\n".join(path.read_text(encoding="utf-8") for path in (root / "v3").glob("*.py"))

    assert "profiles.v3" not in v2
    assert "profiles.v2" not in v3


def test_profile_training_request_rejects_a_head_owned_by_another_profile(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="foreign or duplicate"):
        ProfileTrainingRequest(
            tmp_path / "history",
            tmp_path / "train",
            V2_TRAINING_PROFILE,
            V3_TRAINING_PROFILE.heads,
            LoadProfileTrainingSettings(PROJECT_ROOT, "v2"),
            sample_root=(tmp_path / "train").parent / "samples",
        )


def test_v2_sample_scan_consumes_typed_rows_with_its_profile_horizon(tmp_path: Path) -> None:
    dates = tuple(date(2021, 1, 1) + timedelta(days=index) for index in range(1_600))
    split = BuildBaostockTrainingSplit(dates, parent_manifest_hash="a" * 64)
    requested: list[frozenset[date]] = []

    class Archive:
        snapshot = SimpleNamespace(calendar=BaoStockCalendar(dates))

        @staticmethod
        def training_row_upper_bound(_dates) -> int:
            return 0

        @staticmethod
        def iter_training_rows(readable_dates, progress, verification_progress=None):
            requested.append(readable_dates)
            progress(0)
            return iter(())

    with SQLiteTrainingSampleIndex(tmp_path / "samples.sqlite3", 10) as repository:
        BuildTrainingSamples(
            TrainingSampleBuildRequest(
                Archive(),  # type: ignore[arg-type]
                (),
                TomorrowTrainingWindow(split),
                repository,
                V2_TRAINING_PROFILE,
            )
        )

    assert requested == [TomorrowTrainingWindow(split).readable_dates]


def test_joint_sample_build_reads_history_once_and_matches_independent_profiles(tmp_path: Path) -> None:
    dates = tuple(date(2021, 1, 1) + timedelta(days=index) for index in range(2_000))
    points = tuple(
        HistoryTrainingRow("600000", day, "main", "银行", HistoryTrainingPoint(day, 10.0 + index * 0.01, 1_000_000.0))
        for index, day in enumerate(dates[-1_000:])
    )

    class HistorySourceFixture:
        snapshot = SimpleNamespace(calendar=BaoStockCalendar(dates), active_snapshot_hash="a" * 64)

        def __init__(self) -> None:
            self.scans = 0

        def training_row_upper_bound(self, allowed_dates: frozenset[date]) -> int:
            return sum(row.trade_date in allowed_dates for row in points)

        def iter_training_rows(self, allowed_dates: frozenset[date], progress=None):
            self.scans += 1
            for row in points:
                if row.trade_date in allowed_dates:
                    yield row

    profiles = (V2_TRAINING_PROFILE, V3_TRAINING_PROFILE)
    windows = tuple(
        TomorrowTrainingWindow(
            BuildBaostockTrainingSplit(
                dates,
                parent_manifest_hash="a" * 64,
                fit_dates=1000 if profile.profile_id == "v2" else 750,
                early_stopping_sessions=20 if profile.profile_id == "v2" else 0,
                history_sessions=profile.history_sessions,
            )
        )
        for profile in profiles
    )
    joint_source = HistorySourceFixture()
    with (
        SQLiteTrainingSampleIndex(tmp_path / "joint-v2.sqlite3", 10) as joint_v2,
        SQLiteTrainingSampleIndex(tmp_path / "joint-v3.sqlite3", 6) as joint_v3,
        SQLiteTrainingSampleIndex(tmp_path / "single-v2.sqlite3", 10) as single_v2,
        SQLiteTrainingSampleIndex(tmp_path / "single-v3.sqlite3", 6) as single_v3,
    ):
        joint_requests = tuple(
            TrainingSampleBuildRequest(joint_source, ("600000",), window, repository, profile)
            for window, repository, profile in zip(windows, (joint_v2, joint_v3), profiles, strict=True)
        )
        rows_read = BuildJointTrainingSamples(joint_requests)
        for window, repository, profile in zip(windows, (single_v2, single_v3), profiles, strict=True):
            BuildTrainingSamples(
                TrainingSampleBuildRequest(HistorySourceFixture(), ("600000",), window, repository, profile)
            )
        assert joint_source.scans == 1
        assert rows_read == sum(
            row.trade_date in windows[0].readable_dates | windows[1].readable_dates for row in points
        )
        for joint, single in ((joint_v2, single_v2), (joint_v3, single_v3)):
            assert joint._connection.run("SELECT * FROM samples ORDER BY trade_date, code").fetchall() == (
                single._connection.run("SELECT * FROM samples ORDER BY trade_date, code").fetchall()
            )


def test_date_budget_reserves_warmup_and_every_five_day_label_boundary() -> None:
    dates = tuple(date(2021, 1, 1) + timedelta(days=index) for index in range(2_000))
    split = BuildBaostockTrainingSplit(
        dates, parent_manifest_hash="a" * 64, fit_dates=750, early_stopping_sessions=0, history_sessions=61
    )
    assert len(split.warmup_dates) == 60
    assert len(split.model_fit_dates) == 750
    assert len(split.fit_embargo_dates) == len(split.first_embargo_dates) == 5
    assert len(split.second_embargo_dates) == len(split.terminal_embargo_dates) == 5
    assert split.model_fit_dates[-1] < split.fit_embargo_dates[0] < split.calibration_dates[0]
    assert split.daily_proxy_holdout_dates[-1] < split.terminal_embargo_dates[0] < split.point_in_time_holdout_dates[0]
    assert TomorrowTrainingWindow(split).readable_dates.isdisjoint(split.point_in_time_holdout_dates)

    v2 = BuildBaostockTrainingSplit(
        dates, parent_manifest_hash="a" * 64, fit_dates=1_000, early_stopping_sessions=20, history_sessions=251
    )
    assert len(v2.warmup_dates) == 250
    assert len(v2.model_fit_dates) == 1_000
    assert len(v2.early_stopping_dates) == len(v2.calibration_dates) == 20
    with pytest.raises(ValueError, match="enough unique dates"):
        BuildBaostockTrainingSplit(
            dates[:1_464], parent_manifest_hash="a" * 64, early_stopping_sessions=20, history_sessions=251
        )
    with pytest.raises(ValueError, match="fit dates are insufficient"):
        BuildBaostockTrainingSplit(
            dates, parent_manifest_hash="a" * 64, fit_dates=1_100, early_stopping_sessions=20, history_sessions=251
        )

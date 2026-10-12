from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from trader.download.domain.baostock_daily import BaoStockCalendar, build_baostock_training_split
from trader.recommendation.domain.publication.models import Strategy
from trader.training.application.tomorrow_training import TomorrowTrainingWindow
from trader.training.infra.engine import ProfileTrainingRequest
from trader.training.infra.model_fitting import LightGBMFittingParameters, RidgeFittingParameters
from trader.training.infra.profile.v2.contracts import V2_TRAINING_PROFILE
from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE
from trader.training.infra.sample_builder import TrainingSampleBuildRequest, build_training_samples
from trader.training.infra.sample_repository import SQLiteTrainingSampleRepository
from trader.training.infra.training_settings import load_profile_training_settings
from trader.recommendation.infra.scoring.cost_settings import (
    load_evaluation_cost_scenarios,
    load_execution_cost_policy,
)
from trader.training.infra.research_settings import load_research_settings

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
    v2 = load_profile_training_settings(PROJECT_ROOT, "v2")
    v3 = load_profile_training_settings(PROJECT_ROOT, "v3")
    cost = load_execution_cost_policy(PROJECT_ROOT / "config")
    scenarios = load_evaluation_cost_scenarios(PROJECT_ROOT / "config")
    research = load_research_settings(PROJECT_ROOT / "config")

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
    assert research.deepseek_comparison_arms == ("local_only", "structured_facts_veto", "fixed_68_32")


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
            load_profile_training_settings(PROJECT_ROOT, "v2"),
        )


def test_v2_sample_scan_consumes_typed_rows_with_its_profile_horizon(tmp_path: Path) -> None:
    dates = tuple(date(2021, 1, 1) + timedelta(days=index) for index in range(1_250))
    split = build_baostock_training_split(dates, parent_manifest_hash="a" * 64)
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

    with SQLiteTrainingSampleRepository(tmp_path / "samples.sqlite3", 10) as repository:
        build_training_samples(
            TrainingSampleBuildRequest(
                Archive(),  # type: ignore[arg-type]
                (),
                TomorrowTrainingWindow(split),
                repository,
                V2_TRAINING_PROFILE,
            )
        )

    assert requested == [TomorrowTrainingWindow(split).readable_dates]

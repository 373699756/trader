from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from trader.application.research.tomorrow_training import TomorrowTrainingWindow
from trader.domain.recommendation.models import Strategy
from trader.domain.research.baostock_daily import BaoStockCalendar, build_baostock_training_split
from trader.infra.scoring.profiles.v2.contracts import V2_TRAINING_PROFILE
from trader.infra.scoring.profiles.v3.contracts import V3_TRAINING_PROFILE
from trader.infra.scoring.training.engine import ProfileTrainingRequest
from trader.infra.scoring.training.sample_builder import TrainingSampleBuildRequest, build_training_samples
from trader.infra.scoring.training.sample_repository import SQLiteTrainingSampleRepository


def test_v2_and_v3_share_engine_contract_shape_but_own_their_differences() -> None:
    assert V2_TRAINING_PROFILE.profile_id == "v2"
    assert V2_TRAINING_PROFILE.output_directory == "v2"
    assert V2_TRAINING_PROFILE.history_sessions == 251
    assert V3_TRAINING_PROFILE.profile_id == "v3"
    assert V3_TRAINING_PROFILE.output_directory == "v3"
    assert V3_TRAINING_PROFILE.history_sessions == 61

    assert tuple(head.strategy for head in V2_TRAINING_PROFILE.heads) == (
        Strategy.TODAY,
        Strategy.TOMORROW,
        Strategy.D25,
    )
    assert tuple(head.strategy for head in V3_TRAINING_PROFILE.heads) == (
        Strategy.TODAY,
        Strategy.TOMORROW,
        Strategy.D25,
    )
    assert "qfq_residual_momentum_120d_skip5" in V2_TRAINING_PROFILE.heads[1].feature_manifest.names
    assert "qfq_residual_momentum_250d_skip5" in V2_TRAINING_PROFILE.heads[1].feature_manifest.names
    assert "market_qfq_momentum_120d_skip5" in V2_TRAINING_PROFILE.heads[1].feature_manifest.names
    assert "market_qfq_momentum_250d_skip5" in V2_TRAINING_PROFILE.heads[1].feature_manifest.names
    assert "qfq_residual_momentum_120d_skip5" not in V3_TRAINING_PROFILE.heads[1].feature_manifest.names


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
        )


def test_v2_sample_scan_requests_its_251_session_window(tmp_path: Path) -> None:
    dates = tuple(date(2021, 1, 1) + timedelta(days=index) for index in range(1_250))
    split = build_baostock_training_split(dates, parent_manifest_hash="a" * 64)
    requested: list[int] = []

    class Archive:
        snapshot = SimpleNamespace(calendar=BaoStockCalendar(dates))

        @staticmethod
        def training_row_upper_bound(_dates) -> int:
            return 0

        @staticmethod
        def iter_training_windows(_dates, progress, *, window_sessions=61):
            requested.append(window_sessions)
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

    assert requested == [251]

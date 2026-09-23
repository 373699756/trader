from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_training_online_codec_replay_and_shadow_use_the_catalog_owner() -> None:
    consumers = (
        "src/trader/recommendation/application/pipeline/local_score/model_scoring.py",
        "src/trader/training/application/tomorrow_daily_close_h1.py",
        "src/trader/training/application/tomorrow_historical_screening.py",
        "src/trader/training/application/tomorrow_historical_validation.py",
        "src/trader/training/domain/evaluation/tomorrow_features.py",
        "src/trader/training/domain/tomorrow_training_input.py",
        "src/trader/recommendation/infra/normalization/features.py",
        "src/trader/training/infra/artifacts/contracts.py",
    )
    for relative in consumers:
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "trader.recommendation.domain.market.feature_contracts" in source, relative

    for relative in (
        "src/trader/training/infra/artifacts/bundle_codec.py",
        "src/trader/training/infra/profile/v2/contracts.py",
        "src/trader/training/infra/profile/v3/contracts.py",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "trader.training.infra.artifacts.contracts" in source, relative


def test_v3_feature_identifiers_are_declared_only_by_the_catalog() -> None:
    consumers = (
        "src/trader/recommendation/application/pipeline/local_score/model_scoring.py",
        "src/trader/training/application/tomorrow_daily_close_h1.py",
        "src/trader/training/application/tomorrow_historical_screening.py",
        "src/trader/training/application/tomorrow_historical_validation.py",
        "src/trader/training/domain/tomorrow_training_input.py",
        "src/trader/training/infra/artifacts/bundle_codec.py",
        "src/trader/training/infra/profile/v3/training.py",
    )
    for relative in consumers:
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert '"qfq_residual_momentum_20d_skip5"' not in source, relative


def test_online_and_historical_cost_ranks_share_the_average_rank_owner() -> None:
    consumers = (
        "src/trader/recommendation/application/pipeline/local_score/model_scoring.py",
        "src/trader/training/application/tomorrow_historical_screening.py",
        "src/trader/training/application/tomorrow_historical_validation.py",
    )

    for relative in consumers:
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "percentile_ranks" in source, relative
        assert "def _percentile_ranks" not in source, relative

    owner = (ROOT / "src/trader/recommendation/domain/scoring/utility_scoring.py").read_text(encoding="utf-8")
    assert "average_rank_percentiles" in owner

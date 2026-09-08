from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_training_online_codec_replay_and_shadow_use_the_catalog_owner() -> None:
    consumers = (
        "src/trader/application/recommendation/tomorrow_model_scoring.py",
        "src/trader/application/research/tomorrow_daily_close_h1.py",
        "src/trader/application/research/tomorrow_historical_screening.py",
        "src/trader/application/research/tomorrow_historical_validation.py",
        "src/trader/application/research/tomorrow_profile_holdout.py",
        "src/trader/domain/research/tomorrow_features.py",
        "src/trader/domain/research/tomorrow_training_input.py",
        "src/trader/infra/market_data/normalization/features.py",
        "src/trader/infra/scoring/profiles/v1/artifact_builder.py",
        "src/trader/infra/scoring/profiles/v1/artifact_codec.py",
        "src/trader/infra/scoring/profiles/v2/artifact_codec.py",
        "src/trader/infra/scoring/profiles/v3/bundle_codec.py",
        "src/trader/infra/scoring/profiles/v3/training.py",
    )
    for relative in consumers:
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "trader.domain.market.feature_contracts" in source, relative


def test_v3_feature_identifiers_are_declared_only_by_the_catalog() -> None:
    consumers = (
        "src/trader/application/recommendation/tomorrow_model_scoring.py",
        "src/trader/application/research/tomorrow_daily_close_h1.py",
        "src/trader/application/research/tomorrow_historical_screening.py",
        "src/trader/application/research/tomorrow_historical_validation.py",
        "src/trader/application/research/tomorrow_profile_holdout.py",
        "src/trader/domain/research/tomorrow_training_input.py",
        "src/trader/infra/scoring/profiles/v1/artifact_builder.py",
        "src/trader/infra/scoring/profiles/v1/artifact_codec.py",
        "src/trader/infra/scoring/profiles/v3/bundle_codec.py",
        "src/trader/infra/scoring/profiles/v3/training.py",
    )
    for relative in consumers:
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert '"qfq_residual_momentum_20d_skip5"' not in source, relative

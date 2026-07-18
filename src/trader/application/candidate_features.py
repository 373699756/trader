"""Acquire strategy-specific candidate features and bind their input identity."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime

from trader.application.ports import MarketDataPort
from trader.domain.models import FeatureSnapshot, Strategy


def fetch_strategy_features(
    market_data: MarketDataPort,
    strategy: Strategy,
    codes: Sequence[str],
    observed_at: datetime,
) -> tuple[tuple[FeatureSnapshot, ...], str]:
    features = tuple(
        market_data.fetch_candidate_features(
            codes,
            observed_at,
            include_intraday_tail=strategy is Strategy.TOMORROW,
            include_structured_research=strategy in {Strategy.D25, Strategy.LONG},
        )
    )
    return bind_strategy_input_version(strategy, features)


def read_strategy_features(
    market_data: MarketDataPort,
    strategy: Strategy,
    codes: Sequence[str],
    observed_at: datetime,
) -> tuple[tuple[FeatureSnapshot, ...], str]:
    features = tuple(
        market_data.read_candidate_features(
            codes,
            observed_at,
            include_intraday_tail=strategy is Strategy.TOMORROW,
            include_structured_research=strategy in {Strategy.D25, Strategy.LONG},
        )
    )
    return bind_strategy_input_version(strategy, features)


def bind_strategy_input_version(
    strategy: Strategy,
    features: Sequence[FeatureSnapshot],
) -> tuple[tuple[FeatureSnapshot, ...], str]:
    frozen_features = tuple(features)
    if strategy is Strategy.TODAY:
        return frozen_features, max((feature.quote.data_version for feature in frozen_features), default="unavailable")
    if strategy in {Strategy.D25, Strategy.LONG}:
        research_material = tuple(
            sorted(
                (
                    feature.quote.code,
                    feature.quote.data_version,
                    feature.market_regime,
                    tuple(sorted((name, value) for name, value in feature.values.items())),
                    tuple(
                        sorted(
                            (evidence.evidence_type, evidence.data_version, evidence.evidence_id)
                            for evidence in feature.evidence
                        )
                    ),
                )
                for feature in frozen_features
            )
        )
        digest = hashlib.sha256(repr(research_material).encode("utf-8")).hexdigest()[:20]
        return frozen_features, f"{strategy.value}-input:{digest}"
    tail_material = tuple(
        sorted(
            (
                feature.quote.code,
                feature.quote.data_version,
                tuple(
                    sorted(
                        (evidence.data_version, evidence.evidence_id)
                        for evidence in feature.evidence
                        if evidence.evidence_type == "intraday_tail"
                    )
                ),
            )
            for feature in frozen_features
        )
    )
    digest = hashlib.sha256(repr(tail_material).encode("utf-8")).hexdigest()[:20]
    return frozen_features, f"tomorrow-input:{digest}"


__all__ = ["bind_strategy_input_version", "fetch_strategy_features", "read_strategy_features"]

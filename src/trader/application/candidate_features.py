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
        )
    )
    if strategy is not Strategy.TOMORROW:
        return features, max((feature.quote.data_version for feature in features), default="unavailable")
    material = tuple(
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
            for feature in features
        )
    )
    digest = hashlib.sha256(repr(material).encode("utf-8")).hexdigest()[:20]
    return features, f"tomorrow-input:{digest}"


__all__ = ["fetch_strategy_features"]

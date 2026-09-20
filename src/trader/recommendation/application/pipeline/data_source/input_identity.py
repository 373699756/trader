"""Stable identities for market inputs and quote refreshes."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import TYPE_CHECKING

from trader.recommendation.domain.market.models import FeatureSnapshot

if TYPE_CHECKING:
    from trader.recommendation.application.ports.runtime import CycleRequest


def data_version(
    request: CycleRequest,
    market_features: tuple[FeatureSnapshot, ...],
    candidate_features: tuple[FeatureSnapshot, ...],
) -> str:
    versions = (
        feature_batch_version("market", market_features),
        feature_batch_version("candidate", candidate_features),
    )
    return f"{request.input_version}:{stable_digest(versions)}"


def feature_batch_version(kind: str, features: tuple[FeatureSnapshot, ...]) -> str:
    material = tuple(sorted(feature_identity(feature) for feature in features))
    return f"{kind}:{stable_digest(material)}"


def feature_identity(feature: FeatureSnapshot) -> tuple[object, ...]:
    quote = feature.quote
    return (
        quote.code,
        quote.data_version,
        quote.source_time.isoformat(),
        quote.name,
        quote.industry,
        quote.board.value,
        quote.listing_date.isoformat() if quote.listing_date is not None else None,
        quote.listing_age_sessions,
        quote.execution_restrictions,
        tuple(sorted(feature.values.items())),
        feature.history_days,
        feature.market_regime,
        feature.missing_fields,
        tuple(sorted(feature.missing_reasons.items())),
        tuple((item.evidence_id, item.data_version, item.published_at.isoformat()) for item in feature.evidence),
        tuple(repr(item) for item in feature.external_risk_facts),
        feature.board_policy_version,
        feature.competition_group_version,
        feature.parameter_status,
        feature.selection_skip_reason,
        (
            (
                feature.model_industry.industry_id,
                feature.model_industry.classification,
                feature.model_industry.effective_date.isoformat(),
                feature.model_industry.source,
                feature.model_industry.data_version,
            )
            if feature.model_industry is not None
            else None
        ),
        feature.merge_epoch,
    )


def quote_versions(features: tuple[FeatureSnapshot, ...]) -> dict[str, str]:
    return {
        feature.quote.code: f"{feature.quote.data_version}:{feature.quote.source_time.isoformat()}"
        for feature in features
    }


def changed_version_codes(previous: Mapping[str, str], current: Mapping[str, str]) -> tuple[str, ...]:
    return tuple(sorted(code for code in {*previous, *current} if previous.get(code) != current.get(code)))


def stable_digest(value: object) -> str:
    return hashlib.sha256(repr(value).encode("utf-8")).hexdigest()[:16]

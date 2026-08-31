"""Asynchronous, production-isolated V1/V2 comparison on one native input."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from trader.application.ports.scored import TomorrowNativeInput
from trader.application.recommendation.policy import RecommendationPolicy
from trader.application.recommendation.scored_v2_projection import build_scored_v2_local
from trader.application.recommendation.tomorrow_model_scoring import (
    TomorrowModelDiagnostics,
    TomorrowProductionModelScoringService,
)
from trader.application.research_audit import V2DecisionObservation
from trader.domain.recommendation.decision_identity import DecisionItem
from trader.domain.recommendation.models import Strategy
from trader.domain.research.tomorrow_profile_comparison import (
    TomorrowProfileComparisonSpec,
    TomorrowProfileId,
    TomorrowProfilePair,
    TomorrowProfilePairManifest,
    TomorrowProfilePrediction,
)


@dataclass(frozen=True)
class TomorrowProfileResearchInput:
    source_decision_version: str
    source_decision_hash: str
    active_profile_id: TomorrowProfileId
    native_input: TomorrowNativeInput

    def __post_init__(self) -> None:
        if (
            not self.source_decision_version
            or not self.source_decision_hash
            or self.active_profile_id not in {"v1", "v2"}
            or self.native_input.strategy is not Strategy.TOMORROW
        ):
            raise ValueError("Tomorrow profile research input is invalid")


@dataclass(frozen=True)
class _ProfileProjection:
    items: Mapping[str, DecisionItem]
    diagnostics: Mapping[str, TomorrowModelDiagnostics]
    model_version: str


class TomorrowProfilePairWriter(Protocol):
    def save_manifest(self, manifest: TomorrowProfilePairManifest) -> None: ...


class TomorrowProfileComparator:
    def __init__(
        self,
        spec: TomorrowProfileComparisonSpec,
        policy: RecommendationPolicy,
        v1_model: TomorrowProductionModelScoringService,
        v2_model: TomorrowProductionModelScoringService,
        writer: TomorrowProfilePairWriter,
    ) -> None:
        if v1_model.status().profile_id != "v1" or v2_model.status().profile_id != "v2":
            raise ValueError("Tomorrow profile comparator requires fixed V1 and V2 models")
        self._spec = spec
        self._policy = policy
        self._v1_model = v1_model
        self._v2_model = v2_model
        self._writer = writer

    def record(self, observation: V2DecisionObservation) -> None:
        research_input = observation.tomorrow_profile_input
        if research_input is None:
            return
        self._writer.save_manifest(self.compare(research_input))

    def compare(self, research_input: TomorrowProfileResearchInput) -> TomorrowProfilePairManifest:
        native = research_input.native_input
        v1_projection = build_scored_v2_local(native, self._policy, sequence=1, tomorrow_model=self._v1_model)
        v2_projection = build_scored_v2_local(native, self._policy, sequence=1, tomorrow_model=self._v2_model)
        v1 = _ProfileProjection(
            _items(v1_projection.local.items),
            dict(v1_projection.model_diagnostics),
            v1_projection.score_model_version or "",
        )
        v2 = _ProfileProjection(
            _items(v2_projection.local.items),
            dict(v2_projection.model_diagnostics),
            v2_projection.score_model_version or "",
        )
        features = {feature.quote.code: feature for feature in native.candidate_features}
        common = tuple(sorted(set(v1.diagnostics) & set(v2.diagnostics) & set(v1.items) & set(v2.items)))
        pairs = tuple(_pair(native, code, features[code], v1, v2) for code in common)
        return TomorrowProfilePairManifest(
            spec_hash=self._spec.content_hash,
            input_version=native.input_version,
            trade_date=native.trade_date,
            observed_at=native.evaluated_at,
            active_profile_id=research_input.active_profile_id,
            v1_model_version=v1.model_version,
            v2_model_version=v2.model_version,
            common_candidate_count=len(common),
            v1_scorable_count=len(v1.diagnostics),
            v2_scorable_count=len(v2.diagnostics),
            pairs=pairs,
        )


def _items(values: tuple[DecisionItem, ...]) -> dict[str, DecisionItem]:
    return {item.code: item for item in values}


def _prediction(
    profile_id: TomorrowProfileId,
    model_version: str,
    item: DecisionItem,
    diagnostics: TomorrowModelDiagnostics,
) -> TomorrowProfilePrediction:
    model = item.model_diagnostics
    if model is None:
        raise ValueError("Tomorrow profile decision is missing model diagnostics")
    return TomorrowProfilePrediction(
        profile_id=profile_id,
        model_version=model_version,
        predicted_excess_return_pct=diagnostics.predicted_excess_return_pct,
        estimated_cost_pct=diagnostics.estimated_cost_pct,
        predicted_net_excess_pct=diagnostics.predicted_net_excess_pct,
        signal_score=model.signal_score,
        local_score=item.local_score,
        model_disagreement_pct=diagnostics.model_disagreement_pct,
        action=item.action.value,
        selected=item.selected,
        rank=item.rank,
    )


def _pair(
    native: TomorrowNativeInput,
    code: str,
    feature: object,
    v1: _ProfileProjection,
    v2: _ProfileProjection,
) -> TomorrowProfilePair:
    from trader.domain.market.models import FeatureSnapshot

    if not isinstance(feature, FeatureSnapshot):
        raise TypeError("Tomorrow profile pair feature is invalid")
    price = feature.quote.price
    atr20 = feature.optional_value("atr20_pct")
    if price is None:
        raise ValueError("Tomorrow profile pair requires a point-in-time price")
    return TomorrowProfilePair(
        input_version=native.input_version,
        trade_date=native.trade_date,
        code=code,
        board=feature.quote.board.value,
        industry=feature.quote.industry or "unknown",
        anchor_price=price,
        atr20_pct=atr20,
        v1=_prediction("v1", v1.model_version, v1.items[code], v1.diagnostics[code]),
        v2=_prediction("v2", v2.model_version, v2.items[code], v2.diagnostics[code]),
    )


__all__ = [
    "TomorrowProfileComparator",
    "TomorrowProfilePairWriter",
    "TomorrowProfileResearchInput",
]

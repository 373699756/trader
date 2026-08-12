"""Generic committed-decision event values for V2 observers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from trader.domain.recommendation.decision_identity import DecisionItem, DecisionStage, ScoredDecision
from trader.domain.recommendation.models import RecommendationAction, Strategy


@dataclass(frozen=True)
class V2CommittedDecisionItem:
    code: str
    action: RecommendationAction
    selected: bool
    rank: int
    candidate_score: float | None
    local_score: float
    final_score: float
    score_components: tuple[tuple[str, float | None], ...]
    risk_codes: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class V2DecisionCommitted:
    event_id: str
    strategy: Strategy
    trade_date: date
    observed_at: datetime
    decision_version: str
    decision_hash: str
    parent_version: str | None
    stage: DecisionStage
    input_versions: tuple[tuple[str, str], ...]
    config_version: str
    strategy_version: str
    fusion_version: str
    schema_version: str
    filter_aggregates: tuple[tuple[str, int], ...]
    degraded_reasons: tuple[str, ...]
    items: tuple[V2CommittedDecisionItem, ...]


def build_v2_decision_committed(decision: ScoredDecision) -> V2DecisionCommitted:
    return V2DecisionCommitted(
        event_id=f"v2-decision-committed:{decision.version}",
        strategy=decision.strategy,
        trade_date=decision.trade_date,
        observed_at=decision.observed_at,
        decision_version=decision.version,
        decision_hash=decision.content_hash,
        parent_version=decision.parent_version,
        stage=decision.stage,
        input_versions=decision.input_versions,
        config_version=decision.config_version,
        strategy_version=decision.strategy_version,
        fusion_version=decision.fusion_version,
        schema_version=decision.schema_version,
        filter_aggregates=decision.filter_aggregates,
        degraded_reasons=decision.degraded_reasons,
        items=tuple(_event_item(item) for item in decision.items),
    )


def _event_item(item: DecisionItem) -> V2CommittedDecisionItem:
    return V2CommittedDecisionItem(
        code=item.code,
        action=item.action,
        selected=item.selected,
        rank=item.rank,
        candidate_score=item.candidate_score,
        local_score=item.local_score,
        final_score=item.final_score,
        score_components=item.score_components,
        risk_codes=item.risk_codes,
        reason=item.reason,
    )


__all__ = [
    "V2CommittedDecisionItem",
    "V2DecisionCommitted",
    "build_v2_decision_committed",
]

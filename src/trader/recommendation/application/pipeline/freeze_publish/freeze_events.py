"""Generic committed-freeze event values for observers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date, datetime

from trader.recommendation.application.ports.read_only_queries import ResearchAuditIdentity
from trader.recommendation.domain.publication.freeze_identity import FreezeItem, FreezeStage, ScoredFreeze
from trader.recommendation.domain.publication.models import RecommendationAction, Strategy


@dataclass(frozen=True)
class CommittedFreezeItem:
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
class FreezeCommitted:
    event_id: str
    strategy: Strategy
    trade_date: date
    observed_at: datetime
    freeze_version: str
    freeze_hash: str | None
    parent_version: str | None
    stage: FreezeStage
    input_versions: tuple[tuple[str, str], ...]
    config_version: str
    strategy_version: str
    fusion_version: str
    schema_version: str
    filter_aggregates: tuple[tuple[str, int], ...]
    degraded_reasons: tuple[str, ...]
    items: tuple[CommittedFreezeItem, ...]
    projection_version: str = field(default="", compare=False, repr=False)
    projection: ScoredFreeze | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class FreezeObservation:
    event: FreezeCommitted
    research_audit: ResearchAuditIdentity | None
    audit_factory: Callable[[], ResearchAuditIdentity | None] | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        audit = self.research_audit
        if audit is not None and (
            audit.freeze_version != self.event.freeze_version
            or (self.event.freeze_hash is not None and audit.freeze_hash != self.event.freeze_hash)
        ):
            raise ValueError("research audit must match committed freeze identity")

    def materialize(self) -> FreezeObservation:
        event = self.event
        if event.freeze_hash is None:
            if event.projection is None:
                raise ValueError("research_event_integrity_unavailable")
            event = replace(event, freeze_hash=event.projection.content_hash)
        audit = self.audit_factory() if self.audit_factory is not None else self.research_audit
        return FreezeObservation(event, audit)


def BuildFreezeCommitted(
    freeze: ScoredFreeze,
    *,
    projection_version: str | None = None,
) -> FreezeCommitted:
    return FreezeCommitted(
        event_id=f"freeze-committed:{freeze.version}",
        strategy=freeze.strategy,
        trade_date=freeze.trade_date,
        observed_at=freeze.observed_at,
        freeze_version=freeze.version,
        freeze_hash=None,
        parent_version=freeze.parent_version,
        stage=freeze.stage,
        input_versions=freeze.input_versions,
        config_version=freeze.config_version,
        strategy_version=freeze.strategy_version,
        fusion_version=freeze.fusion_version,
        schema_version=freeze.schema_version,
        filter_aggregates=freeze.filter_aggregates,
        degraded_reasons=freeze.degraded_reasons,
        items=tuple(_event_item(item) for item in freeze.items),
        projection_version=projection_version or freeze.version,
        projection=freeze,
    )


def _event_item(item: FreezeItem) -> CommittedFreezeItem:
    return CommittedFreezeItem(
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
    "CommittedFreezeItem",
    "FreezeCommitted",
    "FreezeObservation",
    "BuildFreezeCommitted",
]

"""Read views backed only by unified V2 scored-decision identities."""

from __future__ import annotations

from datetime import date, datetime, time

from trader.application.decision_core import UnifiedDecisionIndex
from trader.application.ports.clock import Clock
from trader.application.ports.decision_records import DecisionRecordError, DecisionRecordRepositoryPort
from trader.application.tomorrow_views import (
    TomorrowDecisionItemView,
    TomorrowDecisionQueries,
    TomorrowDecisionView,
    TomorrowStatusView,
)
from trader.domain.recommendation.decision_identity import (
    CommittedDecisionRecord,
    DecisionItem,
    ScoredDecision,
)
from trader.domain.recommendation.models import Strategy


class UnifiedScoredDecisionQueries(TomorrowDecisionQueries):
    """Read one scored strategy from its isolated unified V2 partition."""

    def __init__(
        self,
        index: UnifiedDecisionIndex,
        repository: DecisionRecordRepositoryPort,
        clock: Clock,
        *,
        strategy: Strategy,
    ) -> None:
        if strategy is Strategy.LONG:
            raise ValueError("scored decision queries do not support long")
        self._v2_index = index
        self._v2_repository = repository
        self._v2_clock = clock
        self._strategy = strategy

    def current(self) -> TomorrowDecisionView:
        now = self._v2_clock.now()
        snapshot = self._v2_index.snapshot(self._strategy)
        boundary = _freeze_boundary(self._strategy, now)
        formal = snapshot.formal
        if now >= boundary:
            if formal is None or formal.trade_date != now.date():
                return _not_ready(now.date(), ("formal_decision_unavailable",))
            return _view(formal.decision, now=now, formal=formal)
        decision = snapshot.current
        if not isinstance(decision, ScoredDecision) or decision.trade_date != now.date():
            return _not_ready(now.date(), ("current_decision_unavailable",))
        return _view(decision, now=now, formal=None)

    def history(self, trade_date: date) -> TomorrowDecisionView:
        now = self._v2_clock.now()
        try:
            record = self._v2_repository.load(self._strategy, trade_date)
        except (DecisionRecordError, OSError):
            return _not_ready(trade_date, ("history_unavailable",))
        if record is None:
            return _not_ready(trade_date, ("formal_decision_unavailable",))
        return _view(record.decision, now=now, formal=record)

    def status(self) -> TomorrowStatusView:
        now = self._v2_clock.now()
        snapshot = self._v2_index.snapshot(self._strategy)
        boundary = _freeze_boundary(self._strategy, now)
        candidate = snapshot.formal.decision if now >= boundary and snapshot.formal is not None else snapshot.current
        decision = (
            candidate
            if isinstance(candidate, ScoredDecision)
            and candidate.trade_date == now.date()
            and (now < boundary or snapshot.formal is not None)
            else None
        )
        return TomorrowStatusView(
            status="ready" if decision is not None else "not_ready",
            observed_at=now,
            decision_version=decision.version if decision is not None else None,
            decision_trade_date=decision.trade_date.isoformat() if decision is not None else None,
            projection_stage=decision.stage if decision is not None else None,
            quote_version=None,
            decision_age_seconds=max(0.0, (now - decision.observed_at).total_seconds()) if decision else None,
            sources=(),
            pipeline_latency_ms=None,
            publish_latency_ms=None,
            deepseek_limit=168,
            deepseek_used=0,
            deepseek_reserved=0,
            deepseek_remaining=168,
            recent_failures=(),
        )


class UnifiedTomorrowDecisionQueries(UnifiedScoredDecisionQueries):
    """Serve transitional Tomorrow routes from the production V2 identity."""

    def __init__(
        self,
        index: UnifiedDecisionIndex,
        repository: DecisionRecordRepositoryPort,
        clock: Clock,
    ) -> None:
        super().__init__(index, repository, clock, strategy=Strategy.TOMORROW)


def _freeze_boundary(strategy: Strategy, now: datetime) -> datetime:
    boundary_time = time(11, 20) if strategy is Strategy.TODAY else time(14, 50)
    return datetime.combine(now.date(), boundary_time, tzinfo=now.tzinfo)


def _view(
    decision: ScoredDecision,
    *,
    now: datetime,
    formal: CommittedDecisionRecord | None,
) -> TomorrowDecisionView:
    versions = dict(decision.input_versions)
    items = tuple(_item(item, decision.observed_at, now, decision.version) for item in decision.items if item.selected)
    return TomorrowDecisionView(
        status="ready",
        trade_date=decision.trade_date.isoformat(),
        projection_version=decision.version,
        decision_version=decision.version,
        market_epoch_version=versions.get("market", versions.get("native", "unknown")),
        feature_epoch_version=versions.get("candidate"),
        research_epoch_version=versions.get("research"),
        quote_version=None,
        config_version=decision.config_version,
        strategy_version=decision.strategy_version,
        fusion_version=decision.fusion_version,
        projection_stage=decision.stage,
        published_at=decision.observed_at,
        frozen=formal is not None,
        frozen_at=formal.committed_at if formal is not None else None,
        freeze_kind=formal.commit_kind if formal is not None else None,
        freeze_version=formal.version if formal is not None else None,
        data_age_seconds=max(0.0, (now - decision.observed_at).total_seconds()),
        evaluated_count=len(decision.items) + sum(count for _reason, count in decision.filter_aggregates),
        rejected_count=sum(count for _reason, count in decision.filter_aggregates),
        unscored_count=0,
        selected_count=len(items),
        filter_reason_counts=decision.filter_aggregates,
        degraded_reasons=decision.degraded_reasons,
        items=items,
        etag=decision.content_hash,
    )


def _item(
    item: DecisionItem,
    observed_at: datetime,
    now: datetime,
    decision_version: str,
) -> TomorrowDecisionItemView:
    components = dict(item.score_components)
    deepseek_score = components.get("deepseek_score")
    deepseek_penalty = components.get("deepseek_risk_penalty") or 0.0
    return TomorrowDecisionItemView(
        code=item.code,
        name="",
        industry="",
        board="",
        rank=item.rank,
        action=item.action.value,
        action_reason=item.reason,
        disposition="pass",
        current_price=None,
        current_pct_change=None,
        quote_source="decision",
        quote_source_time=observed_at,
        quote_version=decision_version,
        quote_age_seconds=max(0.0, (now - observed_at).total_seconds()),
        anchor_price=None,
        anchor_pct_change=None,
        anchor_source=None,
        anchor_source_time=None,
        anchor_to_now_pct=None,
        local_score=item.local_score,
        deepseek_score=deepseek_score,
        deepseek_risk_penalty=deepseek_penalty,
        final_score=item.final_score,
        fusion_mode="hybrid" if deepseek_score is not None else "local_degraded",
        review_outcome="applied" if deepseek_score is not None else None,
        local_risk_codes=item.risk_codes,
        deepseek_risk_codes=(),
    )


def _not_ready(trade_date: date, reasons: tuple[str, ...]) -> TomorrowDecisionView:
    return TomorrowDecisionView(
        status="not_ready",
        trade_date=trade_date.isoformat(),
        projection_version=None,
        decision_version=None,
        market_epoch_version=None,
        feature_epoch_version=None,
        research_epoch_version=None,
        quote_version=None,
        config_version=None,
        strategy_version=None,
        fusion_version=None,
        projection_stage=None,
        published_at=None,
        frozen=False,
        frozen_at=None,
        freeze_kind=None,
        freeze_version=None,
        data_age_seconds=None,
        evaluated_count=0,
        rejected_count=0,
        unscored_count=0,
        selected_count=0,
        filter_reason_counts=(),
        degraded_reasons=reasons,
        items=(),
        etag=None,
    )


__all__ = ["UnifiedScoredDecisionQueries", "UnifiedTomorrowDecisionQueries"]

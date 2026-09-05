"""Unified read models for every active strategy."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from typing import Literal, Protocol

from trader.application.decisions.decision_core import UnifiedDecisionIndex, UnifiedDecisionSnapshot
from trader.application.decisions.decision_coverage import DecisionCoverage, scored_decision_coverage
from trader.application.decisions.decision_drafts import UnifiedDecisionDraftIndex
from trader.application.ports.clock import Clock
from trader.application.ports.decision_records import DecisionRecordError
from trader.domain.recommendation.decision_identity import (
    CommittedDecisionRecord,
    DecisionItem,
    DecisionOverlay,
    DecisionQuote,
    LongProjection,
    LongProjectionItem,
    ScoredDecision,
    SelectionDiagnostics,
)
from trader.domain.recommendation.models import RecommendationAction, Strategy

DecisionViewStatus = Literal["ready", "not_ready", "not_applicable"]
ScoreStatus = Literal["scored", "not_applicable"]
DECISION_VIEW_SCHEMA_VERSION = "decision_view"


class DecisionHistoryReader(Protocol):
    def load(self, strategy: Strategy, trade_date: date) -> CommittedDecisionRecord | None: ...

    def list_dates(self, strategy: Strategy, *, limit: int = 31) -> tuple[date, ...]: ...


@dataclass(frozen=True)
class DecisionItemView:
    code: str
    name: str
    industry: str
    group: str | None
    selected: bool
    rank: int
    action: str
    action_reason: str
    score_status: ScoreStatus
    candidate_score: float | None
    local_score: float | None
    deepseek_score: float | None
    deepseek_risk_penalty: float | None
    final_score: float | None
    risk_codes: tuple[str, ...]
    price: float | None
    pct_change: float | None
    amount: float | None
    turnover_rate: float | None
    market_cap: float | None
    quote_source: str | None
    quote_time: datetime | None
    quote_status: str
    anchor_price: float | None = None
    anchor_source: str | None = None
    anchor_time: datetime | None = None
    setup_type: str | None = None
    downside_status: str | None = None
    downside_reasons: tuple[str, ...] = ()
    downside_atr20_pct: float | None = None
    downside_intraday_reversal_atr: float | None = None
    downside_historical_drawdown_pct: float | None = None
    review_outcome: str | None = None
    research_evidence_count: int | None = None
    research_risk_fact_count: int | None = None
    review_eligible: bool | None = None
    model_signal_score: float | None = None
    predicted_excess_return_pct: float | None = None
    estimated_cost_pct: float | None = None
    predicted_net_excess_pct: float | None = None
    model_disagreement_pct: float | None = None


@dataclass(frozen=True)
class DecisionDraftView:
    decision_version: str
    content_hash: str
    observed_at: datetime
    items: tuple[DecisionItemView, ...]
    top_scores: tuple[DecisionItemView, ...] = ()


@dataclass(frozen=True)
class DecisionView:
    status: DecisionViewStatus
    strategy: Strategy
    trade_date: date | None
    view: Literal["current", "history"]
    score_status: ScoreStatus
    decision_version: str | None
    content_hash: str | None
    observed_at: datetime | None
    data_age_seconds: float | None
    stage: str | None
    frozen: bool
    frozen_at: datetime | None
    freeze_kind: str | None
    input_versions: tuple[tuple[str, str], ...]
    coverage: DecisionCoverage
    filter_reason_counts: tuple[tuple[str, int], ...]
    degraded_reasons: tuple[str, ...]
    items: tuple[DecisionItemView, ...]
    etag: str | None
    draft: DecisionDraftView | None = None
    selection_diagnostics: SelectionDiagnostics | None = None
    top_scores: tuple[DecisionItemView, ...] = ()
    schema_version: str = DECISION_VIEW_SCHEMA_VERSION

    @property
    def projection_version(self) -> str | None:
        return self.etag


class UnifiedDecisionQueries:
    """Read current identities and immutable formal history without external I/O."""

    def __init__(
        self,
        index: UnifiedDecisionIndex,
        drafts: UnifiedDecisionDraftIndex,
        history: DecisionHistoryReader,
        clock: Clock,
    ) -> None:
        self._index = index
        self._drafts = drafts
        self._history = history
        self._clock = clock

    def current(self, strategy: Strategy) -> DecisionView:
        now = self._clock.now()
        snapshot = self._index.snapshot(strategy)
        if strategy is Strategy.LONG:
            return _long_current(snapshot, now)
        decision, formal = _current_scored(snapshot, strategy, now)
        if decision is None:
            empty = _empty_view(strategy, now.date(), "current", "scored", "current_decision_unavailable")
            draft = _current_draft(self._drafts.snapshot(strategy), strategy, now)
            if draft is None:
                return empty
            draft_view = _observation_draft(draft)
            return replace(empty, draft=draft_view, etag=draft.content_hash)
        return _scored_view(decision, snapshot.overlay, now, "current", formal)

    def history(self, strategy: Strategy, trade_date: date) -> DecisionView:
        now = self._clock.now()
        if strategy is Strategy.LONG:
            return _empty_view(strategy, trade_date, "history", "not_applicable", "history_not_applicable")
        try:
            record = self._history.load(strategy, trade_date)
        except (DecisionRecordError, OSError):
            return _empty_view(strategy, trade_date, "history", "scored", "history_unavailable")
        if record is None:
            return _empty_view(strategy, trade_date, "history", "scored", "formal_decision_unavailable")
        return _scored_view(record.decision, None, now, "history", record)

    def dates(self, strategy: Strategy, *, limit: int = 31) -> tuple[date, ...]:
        if strategy is Strategy.LONG:
            return ()
        try:
            return self._history.list_dates(strategy, limit=limit)
        except (DecisionRecordError, OSError):
            return ()


def _current_scored(
    snapshot: UnifiedDecisionSnapshot,
    strategy: Strategy,
    now: datetime,
) -> tuple[ScoredDecision | None, CommittedDecisionRecord | None]:
    boundary = datetime.combine(
        now.date(),
        time(11, 20) if strategy is Strategy.TODAY else time(14, 50),
        tzinfo=now.tzinfo,
    )
    if now >= boundary:
        formal = snapshot.formal
        if formal is None or formal.trade_date != now.date():
            return None, None
        return formal.decision, formal
    current = snapshot.current
    if not isinstance(current, ScoredDecision) or current.trade_date != now.date():
        return None, None
    return current, None


def _current_draft(decision: ScoredDecision | None, strategy: Strategy, now: datetime) -> ScoredDecision | None:
    if decision is None or decision.strategy is not strategy or decision.trade_date != now.date():
        return None
    boundary = datetime.combine(
        now.date(),
        time(11, 20) if strategy is Strategy.TODAY else time(14, 50),
        tzinfo=now.tzinfo,
    )
    return decision if now < boundary else None


def _observation_draft(decision: ScoredDecision) -> DecisionDraftView:
    selected = sorted(
        (item for item in decision.items if item.selected and item.action is RecommendationAction.OBSERVE),
        key=lambda item: item.rank,
    )
    all_items = tuple(_scored_item(item, None) for item in decision.items)
    top_scores = tuple(sorted(all_items, key=lambda item: (-(item.final_score or 0.0), item.code))[:3])
    return DecisionDraftView(
        decision.version,
        decision.content_hash,
        decision.observed_at,
        tuple(_scored_item(item, None) for item in selected),
        top_scores,
    )


def _scored_view(
    decision: ScoredDecision,
    overlay: DecisionOverlay | None,
    now: datetime,
    view: Literal["current", "history"],
    formal: CommittedDecisionRecord | None,
) -> DecisionView:
    overlay_quotes = _valid_overlay_quotes(decision, overlay)
    selected = tuple(sorted((item for item in decision.items if item.selected), key=lambda item: item.rank))
    all_items = tuple(_scored_item(item, overlay_quotes.get(item.code)) for item in decision.items)
    items = tuple(_scored_item(item, overlay_quotes.get(item.code)) for item in selected)
    top_scores = tuple(
        sorted(
            (item for item in all_items if item.final_score is not None),
            key=lambda item: (-(item.final_score or 0.0), item.code),
        )[:3]
    )
    coverage = scored_decision_coverage(decision)
    etag = _etag(decision.content_hash, overlay.content_hash if overlay_quotes and overlay is not None else None)
    return DecisionView(
        "ready",
        decision.strategy,
        decision.trade_date,
        view,
        "scored",
        decision.version,
        decision.content_hash,
        decision.observed_at,
        max(0.0, (now - decision.observed_at).total_seconds()),
        decision.stage,
        formal is not None,
        formal.committed_at if formal is not None else None,
        formal.commit_kind if formal is not None else None,
        decision.input_versions,
        coverage,
        decision.filter_aggregates,
        decision.degraded_reasons,
        items,
        etag,
        selection_diagnostics=decision.selection_diagnostics,
        top_scores=top_scores,
    )


def _scored_item(item: DecisionItem, quote: DecisionQuote | None) -> DecisionItemView:
    components = dict(item.score_components)
    model = item.model_diagnostics
    display_quote = quote or item.quote
    return DecisionItemView(
        item.code,
        item.name,
        item.industry,
        None,
        item.selected,
        item.rank,
        item.action.value,
        item.reason,
        "scored",
        item.candidate_score,
        item.local_score,
        components.get("deepseek_score"),
        components.get("deepseek_risk_penalty"),
        item.final_score,
        item.risk_codes,
        display_quote.price if display_quote is not None else None,
        display_quote.pct_change if display_quote is not None else None,
        display_quote.amount if display_quote is not None else None,
        display_quote.turnover_rate if display_quote is not None else None,
        display_quote.market_cap if display_quote is not None else None,
        display_quote.source if display_quote is not None else None,
        display_quote.source_time if display_quote is not None else None,
        "live" if quote is not None else "decision_anchor" if item.quote is not None else "missing",
        anchor_price=item.quote.price if item.quote is not None else None,
        anchor_source=item.quote.source if item.quote is not None else None,
        anchor_time=item.quote.source_time if item.quote is not None else None,
        setup_type=item.setup_type,
        downside_status=item.downside.status if item.downside is not None else None,
        downside_reasons=item.downside.reasons if item.downside is not None else (),
        downside_atr20_pct=item.downside.atr20_pct if item.downside is not None else None,
        downside_intraday_reversal_atr=(item.downside.intraday_reversal_atr if item.downside is not None else None),
        downside_historical_drawdown_pct=(item.downside.historical_drawdown_pct if item.downside is not None else None),
        review_outcome=item.review_outcome,
        research_evidence_count=(item.research_coverage.evidence_count if item.research_coverage is not None else None),
        research_risk_fact_count=(
            item.research_coverage.structured_risk_fact_count if item.research_coverage is not None else None
        ),
        review_eligible=(item.research_coverage.review_eligible if item.research_coverage is not None else None),
        model_signal_score=model.signal_score if model is not None else None,
        predicted_excess_return_pct=model.predicted_excess_return_pct if model is not None else None,
        estimated_cost_pct=model.estimated_cost_pct if model is not None else None,
        predicted_net_excess_pct=model.predicted_net_excess_pct if model is not None else None,
        model_disagreement_pct=model.model_disagreement_pct if model is not None else None,
    )


def _long_current(snapshot: UnifiedDecisionSnapshot, now: datetime) -> DecisionView:
    projection = snapshot.current
    if not isinstance(projection, LongProjection):
        return _empty_view(Strategy.LONG, now.date(), "current", "not_applicable", "current_projection_unavailable")
    items = tuple(_long_item(item) for item in projection.items)
    available = sum(item.quote_status != "missing" for item in projection.items)
    coverage = DecisionCoverage(
        candidate_count=len(items),
        evaluated_count=available,
        rejected_count=0,
        selected_count=len(items),
        executable_count=0,
        observation_count=len(items),
    )
    degraded = ("long_quotes_partial",) if available != len(items) else ()
    return DecisionView(
        "ready",
        Strategy.LONG,
        projection.trade_date,
        "current",
        "not_applicable",
        projection.version,
        projection.content_hash,
        projection.observed_at,
        max(0.0, (now - projection.observed_at).total_seconds()),
        "current",
        False,
        None,
        None,
        projection.input_versions,
        coverage,
        (),
        degraded,
        items,
        projection.content_hash,
    )


def _long_item(item: LongProjectionItem) -> DecisionItemView:
    return DecisionItemView(
        item.code,
        item.name,
        item.industry,
        item.group,
        True,
        0,
        "observe",
        "fixed_long_watchlist",
        "not_applicable",
        None,
        None,
        None,
        None,
        None,
        (),
        item.price,
        item.pct_change,
        item.amount,
        item.turnover_rate,
        item.market_cap,
        item.source or None,
        item.source_time,
        item.quote_status,
    )


def _valid_overlay_quotes(decision: ScoredDecision, overlay: DecisionOverlay | None) -> dict[str, DecisionQuote]:
    if (
        overlay is None
        or overlay.strategy is not decision.strategy
        or overlay.trade_date != decision.trade_date
        or overlay.parent_version != decision.version
    ):
        return {}
    return {quote.code: quote for quote in overlay.quotes}


def _empty_view(
    strategy: Strategy,
    trade_date: date,
    view: Literal["current", "history"],
    score_status: ScoreStatus,
    reason: str,
) -> DecisionView:
    return DecisionView(
        "not_applicable" if score_status == "not_applicable" and view == "history" else "not_ready",
        strategy,
        trade_date,
        view,
        score_status,
        None,
        None,
        None,
        None,
        None,
        False,
        None,
        None,
        (),
        DecisionCoverage(0, 0, 0, 0, 0, 0),
        (),
        (reason,),
        (),
        None,
    )


def _etag(content_hash: str, overlay_hash: str | None) -> str:
    if overlay_hash is None:
        return content_hash
    return hashlib.sha256(f"{content_hash}|{overlay_hash}".encode()).hexdigest()


__all__ = [
    "DecisionDraftView",
    "DecisionHistoryReader",
    "DecisionItemView",
    "DecisionView",
    "UnifiedDecisionQueries",
]

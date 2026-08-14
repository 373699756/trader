"""Unified read models for every active V2 strategy."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Literal, Protocol

from trader.application.decision_core import UnifiedDecisionIndex, UnifiedDecisionSnapshot
from trader.application.ports.clock import Clock
from trader.application.ports.decision_records import DecisionRecordError
from trader.domain.recommendation.decision_identity import (
    CommittedDecisionRecord,
    DecisionItem,
    DecisionOverlay,
    LongProjection,
    LongProjectionItem,
    OverlayQuote,
    ScoredDecision,
)
from trader.domain.recommendation.models import Strategy

DecisionViewStatus = Literal["ready", "not_ready", "not_applicable"]
ScoreStatus = Literal["scored", "not_applicable"]


class DecisionHistoryReader(Protocol):
    def load(self, strategy: Strategy, trade_date: date) -> CommittedDecisionRecord | None: ...

    def list_dates(self, strategy: Strategy, *, limit: int = 31) -> tuple[date, ...]: ...


@dataclass(frozen=True)
class DecisionCoverageView:
    candidate_count: int
    evaluated_count: int
    rejected_count: int
    selected_count: int
    executable_count: int
    observation_count: int


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
    coverage: DecisionCoverageView
    filter_reason_counts: tuple[tuple[str, int], ...]
    degraded_reasons: tuple[str, ...]
    items: tuple[DecisionItemView, ...]
    etag: str | None
    schema_version: str = "v2_decision_view_v1"


class UnifiedDecisionQueries:
    """Read current identities and immutable formal history without external I/O."""

    def __init__(
        self,
        index: UnifiedDecisionIndex,
        history: DecisionHistoryReader,
        clock: Clock,
    ) -> None:
        self._index = index
        self._history = history
        self._clock = clock

    def current(self, strategy: Strategy) -> DecisionView:
        now = self._clock.now()
        snapshot = self._index.snapshot(strategy)
        if strategy is Strategy.LONG:
            return _long_current(snapshot, now)
        decision, formal = _current_scored(snapshot, strategy, now)
        if decision is None:
            return _empty_view(strategy, now.date(), "current", "scored", "current_decision_unavailable")
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


def _scored_view(
    decision: ScoredDecision,
    overlay: DecisionOverlay | None,
    now: datetime,
    view: Literal["current", "history"],
    formal: CommittedDecisionRecord | None,
) -> DecisionView:
    overlay_quotes = _valid_overlay_quotes(decision, overlay)
    selected = tuple(item for item in decision.items if item.selected)
    items = tuple(_scored_item(item, overlay_quotes.get(item.code), decision.observed_at) for item in selected)
    rejected = (
        decision.rejected_count
        if decision.rejected_count is not None
        else sum(count for _reason, count in decision.filter_aggregates)
    )
    population = decision.population_count if decision.population_count is not None else len(decision.items) + rejected
    coverage = DecisionCoverageView(
        candidate_count=population,
        evaluated_count=len(decision.items),
        rejected_count=rejected,
        selected_count=len(items),
        executable_count=sum(item.action == "executable" for item in items),
        observation_count=sum(item.action == "observe" for item in items),
    )
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
    )


def _scored_item(item: DecisionItem, quote: OverlayQuote | None, observed_at: datetime) -> DecisionItemView:
    components = dict(item.score_components)
    return DecisionItemView(
        item.code,
        "",
        "",
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
        quote.price if quote is not None else None,
        quote.pct_change if quote is not None else None,
        None,
        None,
        None,
        quote.source if quote is not None else "decision",
        quote.source_time if quote is not None else observed_at,
        "live" if quote is not None else "decision_anchor",
    )


def _long_current(snapshot: UnifiedDecisionSnapshot, now: datetime) -> DecisionView:
    projection = snapshot.current
    if not isinstance(projection, LongProjection):
        return _empty_view(Strategy.LONG, now.date(), "current", "not_applicable", "current_projection_unavailable")
    items = tuple(_long_item(item) for item in projection.items)
    available = sum(item.quote_status != "missing" for item in projection.items)
    coverage = DecisionCoverageView(
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


def _valid_overlay_quotes(decision: ScoredDecision, overlay: DecisionOverlay | None) -> dict[str, OverlayQuote]:
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
        DecisionCoverageView(0, 0, 0, 0, 0, 0),
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
    "DecisionCoverageView",
    "DecisionHistoryReader",
    "DecisionItemView",
    "DecisionView",
    "UnifiedDecisionQueries",
]

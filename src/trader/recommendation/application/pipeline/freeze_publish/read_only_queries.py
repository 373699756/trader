"""Unified read models for every active strategy."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time
from typing import Literal, Protocol

from trader.recommendation.application.pipeline.freeze_publish.draft_index import UnifiedFreezeDraftIndex
from trader.recommendation.application.pipeline.freeze_publish.freeze_coverage import (
    FreezeCoverage,
    ScoredFreezeCoverage,
)
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import (
    UnifiedFreezeIndex,
    UnifiedFreezeSnapshot,
)
from trader.recommendation.application.ports.clock import Clock
from trader.recommendation.application.ports.freeze_records import FreezeRecordError
from trader.recommendation.application.runtime.schedule import SHANGHAI
from trader.recommendation.domain.evidence.document_reference import CompanyDocumentReference
from trader.recommendation.domain.evidence.pipeline import RecommendationPipelineStatus
from trader.recommendation.domain.market.models import MarketQuote, ModelIndustryReference
from trader.recommendation.domain.market.research import ResearchComponentCoverage
from trader.recommendation.domain.publication.freeze_identity import (
    CommittedFreezeRecord,
    FreezeItem,
    FreezeOverlay,
    FreezeQuote,
    FreezeRiskDisclosure,
    LongProjection,
    LongProjectionItem,
    ScoredFreeze,
    SelectionDiagnostics,
    VisibleScoredSeats,
)
from trader.recommendation.domain.publication.historical_pipeline import HistoricalPipelineAudit
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.publication.preview import PreviewContext
from trader.recommendation.domain.publication.score_ledger import ScoreLedger

FreezeViewStatus = Literal["ready", "not_ready", "not_applicable"]
ScoreStatus = Literal["scored", "not_applicable"]
FREEZE_VIEW_SCHEMA_VERSION = "freeze_view"


class FreezeHistoryReader(Protocol):
    def load(self, strategy: Strategy, trade_date: date) -> CommittedFreezeRecord | None: ...

    def list_dates(self, strategy: Strategy, *, limit: int = 31) -> tuple[date, ...]: ...


class CachedQuoteReader(Protocol):
    def cached_quotes(self, codes: Sequence[str]) -> Mapping[str, MarketQuote]: ...


class _IndustryClassificationReader(Protocol):
    def model_industries(self, codes: Sequence[str]) -> Mapping[str, ModelIndustryReference]: ...


@dataclass(frozen=True)
class OutcomeTierSummary:
    tier: Literal["recommend", "watch"]
    horizon: int
    sample_count: int
    complete_count: int
    positive_count: int
    average_net_excess_pct: float | None


class OutcomeSummaryReader(Protocol):
    def summaries(self, strategy: Strategy, *, limit: int = 31) -> tuple[OutcomeTierSummary, ...]: ...


class CompanyDocumentLinkReader(Protocol):
    def document_links(self, code: str, evidence_ids: tuple[str, ...]) -> tuple[CompanyDocumentReference, ...]: ...


@dataclass(frozen=True)
class FreezeQuerySources:
    cached_quotes: CachedQuoteReader | None = None
    outcome_summaries: OutcomeSummaryReader | None = None
    industry_classifications: _IndustryClassificationReader | None = None
    company_documents: CompanyDocumentLinkReader | None = None


@dataclass(frozen=True)
class FreezeItemView:
    code: str
    name: str
    board: str
    industry: str
    group: str | None
    selected: bool
    rank: int
    selection_rank: int
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
    research_components: ResearchComponentCoverage | None = None
    risk_details: tuple[FreezeRiskDisclosure, ...] = ()
    score_ledger: ScoreLedger | None = None
    anchor_daily_return_pct: float | None = None
    anchor_to_now_pct: float | None = None
    classified_industry: str | None = None


@dataclass(frozen=True)
class FreezeDraftView:
    freeze_version: str
    content_hash: str | None
    observed_at: datetime
    input_versions: tuple[tuple[str, str], ...]
    items: tuple[FreezeItemView, ...]
    top_scores: tuple[FreezeItemView, ...] = ()


@dataclass(frozen=True)
class FreezeView:
    status: FreezeViewStatus
    strategy: Strategy
    trade_date: date | None
    view: Literal["current", "history"]
    score_status: ScoreStatus
    freeze_version: str | None
    content_hash: str | None
    observed_at: datetime | None
    data_age_seconds: float | None
    stage: str | None
    frozen: bool
    frozen_at: datetime | None
    freeze_kind: str | None
    input_versions: tuple[tuple[str, str], ...]
    coverage: FreezeCoverage
    filter_reason_counts: tuple[tuple[str, int], ...]
    degraded_reasons: tuple[str, ...]
    items: tuple[FreezeItemView, ...]
    etag: str | None
    draft: FreezeDraftView | None = None
    selection_diagnostics: SelectionDiagnostics | None = None
    top_scores: tuple[FreezeItemView, ...] = ()
    pipeline: RecommendationPipelineStatus | HistoricalPipelineAudit | None = None
    outcome_summaries: tuple[OutcomeTierSummary, ...] = ()
    schema_version: str = FREEZE_VIEW_SCHEMA_VERSION
    preview_context: PreviewContext | None = field(default=None, kw_only=True)

    @property
    def projection_version(self) -> str | None:
        return self.etag


class UnifiedFreezeQueries:
    """Read current identities and immutable formal history without external I/O."""

    def __init__(
        self,
        index: UnifiedFreezeIndex,
        drafts: UnifiedFreezeDraftIndex,
        history: FreezeHistoryReader,
        clock: Clock,
        sources: FreezeQuerySources | None = None,
    ) -> None:
        self._index = index
        self._drafts = drafts
        self._history = history
        self._clock = clock
        self._cached_quotes = sources.cached_quotes if sources is not None else None
        self._outcome_summaries = sources.outcome_summaries if sources is not None else None
        self._industry_classifications = sources.industry_classifications if sources is not None else None
        self._company_documents = sources.company_documents if sources is not None else None

    def company_documents(self, code: str, evidence_ids: tuple[str, ...]) -> tuple[CompanyDocumentReference, ...]:
        if len(code) != 6 or not code.isascii() or not code.isdigit() or not 1 <= len(evidence_ids) <= 32:
            raise ValueError("company_document_query_invalid")
        if any(not identity or len(identity) > 200 for identity in evidence_ids):
            raise ValueError("company_document_identity_invalid")
        if self._company_documents is None:
            return ()
        return self._company_documents.document_links(code, tuple(dict.fromkeys(evidence_ids)))

    def current(self, strategy: Strategy) -> FreezeView:
        now = self._clock.now()
        snapshot = self._index.snapshot(strategy)
        return self._current_view(strategy, snapshot, now)

    @staticmethod
    def current_batch(
        profiles: tuple[tuple[str, UnifiedFreezeQueries], ...],
    ) -> tuple[tuple[str, tuple[FreezeView, FreezeView]], ...]:
        snapshots = UnifiedFreezeIndex.snapshot_batch(tuple(query._index for _, query in profiles))
        now = profiles[0][1]._clock.now()
        return tuple(
            (profile, (
                query._current_view(Strategy.TOMORROW, pair[0], now),
                query._current_view(Strategy.D25, pair[1], now),
            ))
            for (profile, query), pair in zip(profiles, snapshots, strict=True)
        )

    def _current_view(self, strategy: Strategy, snapshot: UnifiedFreezeSnapshot, now: datetime) -> FreezeView:
        if strategy is Strategy.LONG:
            return _long_current(snapshot, now)
        freeze, formal = _current_scored(snapshot, strategy, now)
        if freeze is None:
            empty = _empty_view(strategy, now.date(), "current", "scored", "current_freeze_unavailable")
            draft = _current_draft(self._drafts.snapshot(strategy), strategy, now)
            if draft is None:
                return empty
            classified_industries = self._classified_industries(draft, now)
            draft_view = _observation_draft(draft, classified_industries)
            return replace(empty, draft=draft_view, etag=_classified_etag(draft.version, classified_industries))
        quotes = _valid_overlay_quotes(freeze, snapshot.overlay)
        classified_industries = self._classified_industries(freeze, now)
        view_input = _ScoredViewInput(
            "current",
            quotes,
            snapshot.overlay.version if quotes and snapshot.overlay is not None else None,
            formal,
            classified_industries,
        )
        return _scored_view(freeze, now, view_input)

    def _classified_industries(self, freeze: ScoredFreeze, now: datetime) -> Mapping[str, str]:
        source = self._industry_classifications
        if source is None:
            return {}
        codes = tuple(item.code for item in VisibleScoredSeats(freeze) if not item.industry.strip())
        references = source.model_industries(codes) if codes else {}
        return {
            code: reference.industry_id
            for code, reference in references.items()
            if code in codes and reference.classification == "证监会行业分类" and reference.effective_date <= now.date()
        }

    def history(self, strategy: Strategy, trade_date: date) -> FreezeView:
        now = self._clock.now()
        if strategy is Strategy.LONG:
            return _empty_view(strategy, trade_date, "history", "not_applicable", "history_not_applicable")
        try:
            record = self._history.load(strategy, trade_date)
        except (FreezeRecordError, OSError):
            return _empty_view(strategy, trade_date, "history", "scored", "history_unavailable")
        if record is None:
            return _empty_view(strategy, trade_date, "history", "scored", "formal_freeze_unavailable")
        freeze = record.freeze
        selected = tuple(item for item in freeze.items if item.selected)
        top = VisibleScoredSeats(freeze)
        codes = tuple(sorted({item.code for item in (*selected, *top)}))
        cached = self._cached_quotes.cached_quotes(codes) if self._cached_quotes is not None else {}
        anchors = {item.code: item.quote for item in (*selected, *top)}
        quotes: dict[str, FreezeQuote] = {}
        for code, quote in cached.items():
            anchor = anchors.get(code)
            price = quote.price
            if quote.source_time.tzinfo is None or quote.source_time.utcoffset() is None:
                continue
            source_time = quote.source_time.astimezone(SHANGHAI)
            if (
                anchor is None
                or price is None
                or not math.isfinite(price)
                or price <= 0
                or source_time <= anchor.source_time
                or source_time.date() != now.date()
                or source_time > now
            ):
                continue
            quotes[code] = FreezeQuote(
                code,
                price,
                quote.pct_change,
                quote.amount,
                quote.turnover_rate,
                quote.market_cap,
                quote.source,
                source_time,
                quote.data_version,
            )
        view = _scored_view(freeze, now, _ScoredViewInput("history", quotes, None, record))
        if self._outcome_summaries is None:
            return view
        try:
            return replace(view, outcome_summaries=self._outcome_summaries.summaries(strategy))
        except (OSError, RuntimeError, ValueError):
            return replace(view, degraded_reasons=(*view.degraded_reasons, "outcome_summary_unavailable"))

    def dates(self, strategy: Strategy, *, limit: int = 31) -> tuple[date, ...]:
        if strategy is Strategy.LONG:
            return ()
        try:
            return self._history.list_dates(strategy, limit=limit)
        except (FreezeRecordError, OSError):
            return ()


def _current_scored(
    snapshot: UnifiedFreezeSnapshot,
    strategy: Strategy,
    now: datetime,
) -> tuple[ScoredFreeze | None, CommittedFreezeRecord | None]:
    current = snapshot.current
    if isinstance(current, ScoredFreeze) and current.strategy is strategy and current.preview_context is not None:
        return current, None
    boundary = datetime.combine(
        now.date(),
        time(15, 0),
        tzinfo=now.tzinfo,
    )
    if now >= boundary:
        formal = snapshot.formal
        if formal is None or formal.trade_date != now.date():
            return None, None
        return formal.freeze, formal
    if not isinstance(current, ScoredFreeze) or current.trade_date != now.date():
        return None, None
    return current, None


def _current_draft(freeze: ScoredFreeze | None, strategy: Strategy, now: datetime) -> ScoredFreeze | None:
    if freeze is None or freeze.strategy is not strategy or freeze.trade_date != now.date():
        return None
    boundary = datetime.combine(
        now.date(),
        time(15, 0),
        tzinfo=now.tzinfo,
    )
    return freeze if now < boundary else None


def _observation_draft(freeze: ScoredFreeze, classified_industries: Mapping[str, str]) -> FreezeDraftView:
    top_scores = tuple(
        replace(_scored_item(item, None, classified_industry=classified_industries.get(item.code)), rank=rank)
        for rank, item in enumerate(VisibleScoredSeats(freeze), start=1)
    )
    return FreezeDraftView(
        freeze.version,
        None,
        freeze.observed_at,
        freeze.input_versions,
        top_scores,
        top_scores,
    )


@dataclass(frozen=True)
class _ScoredViewInput:
    view: Literal["current", "history"]
    quotes: Mapping[str, FreezeQuote]
    overlay_version: str | None
    formal: CommittedFreezeRecord | None
    classified_industries: Mapping[str, str] = field(default_factory=dict)


def _scored_view(freeze: ScoredFreeze, now: datetime, view_input: _ScoredViewInput) -> FreezeView:
    overlay_quotes = view_input.quotes
    view = view_input.view
    formal = view_input.formal
    top_scores = tuple(
        replace(
            _scored_item(
                item,
                overlay_quotes.get(item.code),
                historical=view == "history",
                classified_industry=view_input.classified_industries.get(item.code),
            ),
            rank=rank,
        )
        for rank, item in enumerate(VisibleScoredSeats(freeze), start=1)
    )
    items = top_scores
    coverage = ScoredFreezeCoverage(freeze)
    etag = _classified_etag(_etag(freeze.version, view_input.overlay_version), view_input.classified_industries)
    if view == "history":
        quote_identity = [
            (code, quote.price, quote.pct_change, quote.source, quote.source_time.isoformat(), quote.data_version)
            for code, quote in sorted(overlay_quotes.items())
        ]
        digest = hashlib.sha256(
            json.dumps(quote_identity, ensure_ascii=True, separators=(",", ":")).encode()
        ).hexdigest()[:16]
        etag = f"{freeze.version}+history:{digest}"
    return FreezeView(
        "ready",
        freeze.strategy,
        freeze.trade_date,
        view,
        "scored",
        freeze.version,
        None,
        freeze.observed_at,
        max(0.0, (now - freeze.observed_at).total_seconds()),
        freeze.stage,
        formal is not None,
        formal.committed_at if formal is not None else None,
        formal.commit_kind if formal is not None else None,
        freeze.input_versions,
        coverage,
        freeze.filter_aggregates,
        freeze.degraded_reasons,
        items,
        etag,
        selection_diagnostics=freeze.selection_diagnostics,
        top_scores=top_scores,
        pipeline=freeze.pipeline,
        preview_context=freeze.preview_context,
    )


def _scored_item(
    item: FreezeItem,
    quote: FreezeQuote | None,
    *,
    historical: bool = False,
    classified_industry: str | None = None,
) -> FreezeItemView:
    components = dict(item.score_components)
    model = item.model_diagnostics
    display_quote = quote if historical else quote or item.quote
    return FreezeItemView(
        item.code,
        item.name,
        item.board.value,
        item.industry,
        None,
        item.selected,
        item.rank,
        item.selection_rank,
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
        "live"
        if quote is not None
        else "missing"
        if historical
        else "freeze_anchor"
        if item.quote is not None
        else "missing",
        anchor_price=item.quote.price if item.quote is not None else None,
        anchor_source=item.quote.source if item.quote is not None else None,
        anchor_time=item.quote.source_time if item.quote is not None else None,
        anchor_daily_return_pct=item.quote.pct_change if item.quote is not None else None,
        anchor_to_now_pct=(
            (quote.price / item.quote.price - 1.0) * 100.0
            if historical and quote is not None and item.quote is not None
            else None
        ),
        risk_details=item.risk_details,
        score_ledger=item.score_ledger,
        research_components=(item.research_coverage.components if item.research_coverage is not None else None),
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
        classified_industry=classified_industry,
    )


def _long_current(snapshot: UnifiedFreezeSnapshot, now: datetime) -> FreezeView:
    projection = snapshot.current
    if not isinstance(projection, LongProjection):
        return _empty_view(Strategy.LONG, now.date(), "current", "not_applicable", "current_projection_unavailable")
    items = tuple(_long_item(item) for item in projection.items)
    available = sum(item.quote_status != "missing" for item in projection.items)
    coverage = FreezeCoverage(
        candidate_count=len(items),
        evaluated_count=available,
        rejected_count=0,
        selected_count=len(items),
        executable_count=0,
        observation_count=len(items),
    )
    degraded = ("long_quotes_partial",) if available != len(items) else ()
    return FreezeView(
        "ready",
        Strategy.LONG,
        projection.trade_date,
        "current",
        "not_applicable",
        projection.version,
        None,
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
        projection.version,
    )


def _long_item(item: LongProjectionItem) -> FreezeItemView:
    return FreezeItemView(
        item.code,
        item.name,
        "unsupported",
        item.industry,
        item.group,
        True,
        0,
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


def _valid_overlay_quotes(freeze: ScoredFreeze, overlay: FreezeOverlay | None) -> dict[str, FreezeQuote]:
    if (
        overlay is None
        or overlay.strategy is not freeze.strategy
        or overlay.trade_date != freeze.trade_date
        or overlay.parent_version != freeze.version
    ):
        return {}
    return {quote.code: quote for quote in overlay.quotes}


def _top_score_order(item: FreezeItemView) -> tuple[float, float, str]:
    return (-(item.final_score or 0.0), -(item.local_score or 0.0), item.code)


def _empty_view(
    strategy: Strategy,
    trade_date: date,
    view: Literal["current", "history"],
    score_status: ScoreStatus,
    reason: str,
) -> FreezeView:
    return FreezeView(
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
        FreezeCoverage(0, 0, 0, 0, 0, 0),
        (),
        (reason,),
        (),
        None,
    )


def _etag(freeze_version: str, overlay_version: str | None) -> str:
    if overlay_version is None:
        return freeze_version
    return f"{freeze_version}+{overlay_version}"


def _classified_etag(base: str, classified_industries: Mapping[str, str]) -> str:
    if not classified_industries:
        return base
    identity = tuple(sorted(classified_industries.items()))
    digest = hashlib.sha256(json.dumps(identity, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()[:16]
    return f"{base}+industry:{digest}"


__all__ = [
    "CachedQuoteReader",
    "FreezeQuerySources",
    "FreezeDraftView",
    "FreezeHistoryReader",
    "FreezeItemView",
    "FreezeView",
    "UnifiedFreezeQueries",
]

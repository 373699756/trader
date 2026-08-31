"""Read-only scored-strategy feature assembly and deterministic local selection."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime

from trader.application.policy import RecommendationPolicy
from trader.application.ports.market import DataPlaneReadPort, MarketDataPlaneSnapshot
from trader.domain.market.epochs import (
    CandidateFeatureRow,
    CandidateQuoteEpoch,
    DailyFeatureRow,
    MarketEpoch,
    ResearchEpoch,
)
from trader.domain.market.models import Board, Evidence, FeatureSnapshot, LiveQuote, MarketQuote
from trader.domain.market.research import ResearchObservation, derive_corporate_risk_features
from trader.domain.recommendation.models import Strategy
from trader.domain.recommendation.ranking import minimum_selection_score
from trader.domain.recommendation.scored_selection import (
    BoardCrossSectionFallback,
    ScoredSelectionPolicy,
    ScoredSelectionRequest,
    ScoredSelectionResult,
    select_scored,
)
from trader.domain.recommendation.strategies.composition import LocalScoreResult

_SUPPORTED_BOARDS = (Board.MAIN, Board.CHINEXT, Board.STAR)


@dataclass(frozen=True)
class ScoredSelectionOptions:
    evaluated_at: datetime
    max_age_seconds: float
    phase: str = "tomorrow"
    fallbacks: Mapping[Board, BoardCrossSectionFallback] | None = None
    candidate_features: tuple[FeatureSnapshot, ...] | None = None
    normalize_discovery_source_time: bool = False
    strategy: Strategy = Strategy.TOMORROW
    population_evaluated_at: datetime | None = None
    population_max_age_seconds: float | None = None
    minimum_history_sessions: int = 20


@dataclass(frozen=True)
class ScoredSelectionIdentity:
    trade_date: date
    data_version: str
    merge_epoch: str


@dataclass(frozen=True)
class _AssemblyContext:
    market: MarketEpoch
    candidate_epoch: CandidateQuoteEpoch | None
    research_epoch: ResearchEpoch | None
    rows: Mapping[str, DailyFeatureRow]
    quotes: Mapping[str, MarketQuote]
    candidate_quotes: Mapping[str, LiveQuote]
    candidate_features: Mapping[str, CandidateFeatureRow]
    research_observations: Mapping[str, ResearchObservation]
    merge_epoch: str


class ScoredSelectionNotReadyError(RuntimeError):
    """A coherent current market epoch is not available for local selection."""


class ScoredSelectionUseCase:
    def __init__(self, reader: DataPlaneReadPort, policy: RecommendationPolicy) -> None:
        self._reader = reader
        self._policy = policy

    def execute(
        self,
        *,
        evaluated_at: datetime,
        max_age_seconds: float,
        phase: str = "tomorrow",
        fallbacks: Mapping[Board, BoardCrossSectionFallback] | None = None,
    ) -> ScoredSelectionResult:
        snapshot = self._reader.snapshot()
        return select_scored_snapshot(
            snapshot,
            self._policy,
            ScoredSelectionOptions(
                evaluated_at=evaluated_at,
                max_age_seconds=max_age_seconds,
                phase=phase,
                fallbacks=fallbacks,
            ),
        )


def select_scored_snapshot(
    snapshot: MarketDataPlaneSnapshot,
    policy: RecommendationPolicy,
    options: ScoredSelectionOptions,
) -> ScoredSelectionResult:
    evaluated_at = options.evaluated_at
    if snapshot.daily_features is None or snapshot.market is None:
        raise ScoredSelectionNotReadyError("coherent_market_epoch_unavailable")
    if evaluated_at.date() != snapshot.market.trade_date:
        raise ScoredSelectionNotReadyError("market_epoch_trade_date_mismatch")
    epoch_times = [
        snapshot.daily_features.observed_at,
        snapshot.daily_features.received_at,
        snapshot.market.observed_at,
        snapshot.market.received_at,
    ]
    if snapshot.candidate_quotes is not None:
        epoch_times.extend(
            (
                snapshot.candidate_quotes.observed_at,
                snapshot.candidate_quotes.received_at,
            )
        )
    if snapshot.research is not None:
        epoch_times.extend((snapshot.research.observed_at, snapshot.research.received_at))
    if max(epoch_times) > evaluated_at:
        raise ScoredSelectionNotReadyError("market_epoch_from_future")
    features = assemble_scored_features(snapshot)
    merge_epochs = {feature.merge_epoch for feature in features}
    if len(merge_epochs) != 1:
        raise ScoredSelectionNotReadyError("feature_merge_epoch_mismatch")
    return select_scored_features(
        features,
        policy,
        options,
        ScoredSelectionIdentity(
            trade_date=snapshot.market.trade_date,
            data_version=snapshot.market.content_hash,
            merge_epoch=next(iter(merge_epochs)),
        ),
    )


def select_scored_features(
    features: Sequence[FeatureSnapshot],
    policy: RecommendationPolicy,
    options: ScoredSelectionOptions,
    identity: ScoredSelectionIdentity,
    *,
    local_score_overrides: Mapping[str, LocalScoreResult] | None = None,
) -> ScoredSelectionResult:
    """Select scored candidates from an already coherent point-in-time population."""

    evaluated_at = options.evaluated_at
    population = tuple(features)
    if evaluated_at.date() != identity.trade_date:
        raise ScoredSelectionNotReadyError("market_epoch_trade_date_mismatch")
    if not population:
        raise ScoredSelectionNotReadyError("coherent_market_epoch_unavailable")
    population_evaluated_at = options.population_evaluated_at or evaluated_at
    if options.normalize_discovery_source_time:
        population = tuple(
            replace(
                feature,
                quote=replace(
                    feature.quote,
                    source_time=min(population_evaluated_at, feature.quote.received_time),
                ),
            )
            for feature in population
        )
    return select_scored(
        ScoredSelectionRequest(
            features=population,
            evaluated_at=evaluated_at,
            trade_date=identity.trade_date.isoformat(),
            phase=options.phase,
            data_version=identity.data_version,
            merge_epoch=identity.merge_epoch,
            policy=_selection_policy(policy, options),
            candidate_features=options.candidate_features,
            fallbacks=options.fallbacks or {},
            local_score_overrides=local_score_overrides,
            population_evaluated_at=population_evaluated_at,
            population_max_age_seconds=options.population_max_age_seconds,
            minimum_history_sessions=options.minimum_history_sessions,
        )
    )


def _selection_policy(
    policy: RecommendationPolicy,
    options: ScoredSelectionOptions,
) -> ScoredSelectionPolicy:
    board_policies = {
        board: board_policy
        for board in _SUPPORTED_BOARDS
        if (board_policy := policy.board_policy(options.strategy, board)) is not None
    }
    minimum_score = minimum_selection_score(
        options.strategy,
        policy.selection.thresholds,
        phase=options.phase,
        observation_margin=policy.selection.observation_margin,
    )
    return ScoredSelectionPolicy(
        board_policies=board_policies,
        risk_rules=policy.risk_rules,
        max_age_seconds=options.max_age_seconds,
        local_risk_cap=policy.fusion.local_risk_cap,
        candidate_limit_per_board=120,
        top_k=min(policy.selection.default_top_k, 10),
        maximum_per_industry=policy.selection.maximum_per_industry,
        minimum_local_score=minimum_score if minimum_score is not None else 100.0,
        hard_filter=policy.hard_filter,
        strategy=options.strategy,
    )


def assemble_scored_features(snapshot: MarketDataPlaneSnapshot) -> tuple[FeatureSnapshot, ...]:
    context = _assembly_context(snapshot)
    return tuple(_assemble_feature(context, code) for code in sorted(context.quotes))


def _assembly_context(snapshot: MarketDataPlaneSnapshot) -> _AssemblyContext:
    daily = snapshot.daily_features
    market = snapshot.market
    if daily is None or market is None:
        raise ScoredSelectionNotReadyError("coherent_market_epoch_unavailable")
    rows = {row.code: row for row in daily.rows}
    quotes = {quote.code: quote for quote in market.quotes}
    candidate_epoch = snapshot.candidate_quotes
    candidate_quotes = {quote.code: quote for quote in candidate_epoch.quotes} if candidate_epoch is not None else {}
    candidate_features = {row.code: row for row in candidate_epoch.feature_rows} if candidate_epoch is not None else {}
    research_epoch = snapshot.research
    research_observations = research_epoch.observations if research_epoch is not None else {}
    unknown_candidates = sorted(set(candidate_quotes).difference(quotes))
    if unknown_candidates:
        raise ScoredSelectionNotReadyError("candidate_quote_not_in_market_epoch")
    candidate_epoch_is_effective = any(
        quote.source_time >= quotes[code].source_time for code, quote in candidate_quotes.items()
    )
    merge_epoch = market.version
    if candidate_epoch_is_effective and candidate_epoch is not None:
        merge_epoch = f"{merge_epoch}|{candidate_epoch.version}"
    research_epoch_is_effective = any(code in quotes for code in research_observations)
    if research_epoch_is_effective and research_epoch is not None:
        merge_epoch = f"{merge_epoch}|{research_epoch.version}"
    return _AssemblyContext(
        market=market,
        candidate_epoch=candidate_epoch,
        research_epoch=research_epoch,
        rows=rows,
        quotes=quotes,
        candidate_quotes=candidate_quotes,
        candidate_features=candidate_features,
        research_observations=research_observations,
        merge_epoch=merge_epoch,
    )


def _assemble_feature(context: _AssemblyContext, code: str) -> FeatureSnapshot:
    market_quote = context.quotes[code]
    candidate_quote = context.candidate_quotes.get(code)
    candidate_is_current = candidate_quote is not None and candidate_quote.source_time >= market_quote.source_time
    row = context.rows.get(code)
    values, missing_fields, missing_reasons = _daily_values(row)
    candidate_row = context.candidate_features.get(code) if candidate_is_current else None
    if candidate_row is not None:
        _apply_candidate_values(values, missing_fields, missing_reasons, candidate_row)
    research = context.research_observations.get(code)
    evidence = [_market_evidence(context.market, market_quote)]
    observed_times = [context.market.observed_at, context.market.received_at]
    if candidate_is_current and context.candidate_epoch is not None and candidate_quote is not None:
        evidence.append(_candidate_evidence(context.candidate_epoch, candidate_quote))
        observed_times.extend(
            (
                context.candidate_epoch.observed_at,
                context.candidate_epoch.received_at,
            )
        )
    if research is not None and context.research_epoch is not None:
        _apply_research_risk_values(values, research, context.research_epoch.observed_at)
        evidence.extend(research.evidence)
        observed_times.extend(
            (
                context.research_epoch.observed_at,
                context.research_epoch.received_at,
            )
        )
    return FeatureSnapshot(
        quote=_apply_candidate_quote(market_quote, candidate_quote),
        values=values,
        observed_at=max(observed_times),
        history_days=row.history_sessions if row is not None else 0,
        market_regime=context.market.market_regime,
        missing_fields=tuple(sorted(missing_fields)),
        missing_reasons=missing_reasons,
        evidence=_unique_evidence(evidence),
        merge_epoch=context.merge_epoch,
    )


def _daily_values(
    row: DailyFeatureRow | None,
) -> tuple[dict[str, float | None], set[str], dict[str, str]]:
    if row is None:
        return {}, {"daily_features"}, {"daily_features": "daily feature row is unavailable"}
    return dict(row.values), set(row.missing_fields), dict(row.missing_reasons)


def _apply_candidate_values(
    values: dict[str, float | None],
    missing_fields: set[str],
    missing_reasons: dict[str, str],
    candidate: CandidateFeatureRow,
) -> None:
    values.update(candidate.values)
    for name, value in candidate.values.items():
        if value is None:
            missing_fields.add(name)
            missing_reasons.setdefault(name, "candidate realtime feature is unavailable")
        else:
            missing_fields.discard(name)
            missing_reasons.pop(name, None)
    missing_fields.update(candidate.missing_fields)
    missing_reasons.update(candidate.missing_reasons)


def _apply_research_risk_values(
    values: dict[str, float | None],
    research: ResearchObservation,
    observed_at: datetime,
) -> None:
    current = derive_corporate_risk_features(
        research.corporate_risk_facts,
        observed_at,
        history_complete=research.corporate_risk_history_complete,
    )
    if research.corporate_risk_history_complete:
        values.update(current)
        return
    for name, value in current.items():
        if name == "corporate_risk_history_unavailable":
            values[name] = 1.0
        elif value:
            values[name] = max(values.get(name) or 0.0, value)


def _market_evidence(market: MarketEpoch, quote: MarketQuote) -> Evidence:
    return Evidence(
        evidence_id=f"market:{quote.code}:{market.content_hash[:16]}",
        evidence_type="structured_point_in_time",
        title="Point-in-time canonical market quote",
        source=quote.source,
        published_at=quote.source_time,
        received_at=quote.received_time,
        data_version=quote.data_version,
    )


def _candidate_evidence(candidate_epoch: CandidateQuoteEpoch, quote: LiveQuote) -> Evidence:
    return Evidence(
        evidence_id=f"tail:{quote.code}:{candidate_epoch.content_hash[:16]}",
        evidence_type="intraday_tail",
        title="Verified candidate tail quote and intraday structure",
        source=quote.source,
        published_at=quote.source_time,
        received_at=quote.received_time,
        data_version=quote.data_version,
    )


def _unique_evidence(evidence: list[Evidence]) -> tuple[Evidence, ...]:
    by_id: dict[str, Evidence] = {}
    for item in evidence:
        by_id.setdefault(item.evidence_id, item)
    return tuple(sorted(by_id.values(), key=lambda item: item.evidence_id))


def _apply_candidate_quote(market: MarketQuote, candidate: LiveQuote | None) -> MarketQuote:
    if candidate is None or candidate.source_time < market.source_time:
        return market
    high = max(value for value in (market.high, candidate.price) if value is not None)
    low = min(value for value in (market.low, candidate.price) if value is not None)
    return replace(
        market,
        price=candidate.price,
        high=high,
        low=low,
        pct_change=candidate.pct_change,
        source=candidate.source,
        source_time=candidate.source_time,
        received_time=candidate.received_time,
        data_version=candidate.data_version,
        cross_source_deviation_pct=candidate.cross_source_deviation_pct,
        cross_source_verified=candidate.cross_source_verified,
    )


__all__ = [
    "ScoredSelectionNotReadyError",
    "ScoredSelectionOptions",
    "ScoredSelectionUseCase",
    "assemble_scored_features",
    "select_scored_features",
    "select_scored_snapshot",
]

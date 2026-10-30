"""Deterministic complete model inputs for offline recommendation performance probes."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import cast

from trader.infra.cache_contracts import CanonicalJsonBytes
from trader.infra.market_data.history.history import BuildHistoryContext, DailyBar, HistoryContext, PriceAdjustment
from trader.infra.market_data.observations import SourceObservation
from trader.infra.settings.models import PerformanceBudgetSettings
from trader.recommendation.application.pipeline.candidate_pool.candidate_builder import (
    SCORED_STRATEGIES,
    CandidatePlanSet,
)
from trader.recommendation.application.ports.loaded_profile import LoadedScoringProfile, ModelPredictorPort
from trader.recommendation.application.runtime.schedule import SHANGHAI
from trader.recommendation.domain.market.feature_contracts import CalculateProfileQfqAlpha, QfqPriceAnchors
from trader.recommendation.domain.market.history_alignment import ModelHistoryQuality
from trader.recommendation.domain.market.models import Board, FeatureSnapshot, MarketQuote, ModelIndustryReference
from trader.recommendation.domain.market.v1.features import (
    LEARNED_RANK_FEATURE_IDS,
    CalculateLearnedRankFeatures,
    LearnedRankDailyPoint,
)
from trader.recommendation.domain.market.v5.features import CalculatePriceVolumeSequence, PriceVolumePoint
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.selection.scored_selection import (
    ScoredCandidatePlan,
    ScoredCandidateStageCounts,
)
from trader.recommendation.infra.normalization.merge import ObservationFromQuote
from trader.recommendation.infra.normalization.quote import BuildMarketQuote, MarketQuoteInput

_FIXTURE_SOURCE = "performance-completed-history"


@dataclass(frozen=True)
class PerformanceHistoryInput:
    code: str
    bars: tuple[DailyBar, ...]
    context: HistoryContext
    model_feature: FeatureSnapshot


def BuildPerformanceHistory(features: tuple[FeatureSnapshot, ...]) -> tuple[PerformanceHistoryInput, ...]:
    """Reuse the model fixture's daily generator; retain the production-sized raw tail."""
    if not features:
        raise ValueError("performance history requires a population")
    days = _completed_dates(features[0].observed_at.date(), max(feature.history_days for feature in features))
    result: list[PerformanceHistoryInput] = []
    for position, feature in enumerate(features):
        points = _history_points(feature.quote, position, days)[-feature.history_days :]
        bars = tuple(
            DailyBar(
                point.trade_date.isoformat(),
                point.open_price,
                point.close,
                point.high,
                point.low,
                point.volume,
                point.amount,
                0.0,
                1.5,
                adjustment=PriceAdjustment.QFQ,
                source=_FIXTURE_SOURCE,
            )
            for point in points
        )
        result.append(
            PerformanceHistoryInput(
                feature.quote.code,
                bars[-20:],
                BuildHistoryContext(bars, lookback_sessions=feature.history_days),
                feature,
            )
        )
    return tuple(result)


def BuildPerformanceFixtures(
    budgets: PerformanceBudgetSettings,
    profile: LoadedScoringProfile,
    *,
    history_sessions: int,
    rows: int | None = None,
) -> tuple[tuple[MarketQuoteInput, ...], tuple[MarketQuote, ...], tuple[FeatureSnapshot, ...]]:
    """Build every profile's complete synthetic workload outside measured operations."""

    now = datetime(2026, 8, 25, 14, 50, tzinfo=SHANGHAI)
    market_rows = budgets.workload.market_rows if rows is None else rows
    candidate_rows = budgets.workload.candidate_rows if rows is None else rows
    if history_sessions < 1:
        raise ValueError("performance fixture history sessions must be positive")
    if not 1 <= market_rows <= budgets.workload.market_rows or not 1 <= candidate_rows <= market_rows:
        raise ValueError("performance fixture rows must be within the configured population")
    industries = _shared_industries(profile)
    market = tuple(
        replace(_quote_input(index, now), industry=industries[index % len(industries)]) for index in range(market_rows)
    )
    quotes = tuple(_complete_quote(BuildMarketQuote(item), now) for item in market)
    days = _completed_dates(now.date(), history_sessions)
    candidates = tuple(
        _model_feature(quote, index, now, days, profile, history_sessions)
        for index, quote in enumerate(quotes[:candidate_rows])
    )
    return market, quotes, candidates


def _shared_industries(profile: LoadedScoringProfile) -> tuple[str, ...]:
    if set(profile.heads) != {Strategy.TOMORROW, Strategy.D25}:
        raise ValueError("performance_profile_requires_complete_heads")
    if profile.profile_id == "v1":
        return tuple(f"performance-industry-{index}" for index in range(20))
    coverage = tuple(set(cast(ModelPredictorPort, head.predictor).industry_ids) for head in profile.heads.values())
    shared = tuple(sorted(set.intersection(*coverage)))
    if not shared:
        raise ValueError("performance_profile_common_industry_coverage_missing")
    return shared


def _completed_dates(before: date, count: int) -> tuple[date, ...]:
    """A fixed synthetic weekday calendar, never a claim about supplier history."""

    days: list[date] = []
    day = before - timedelta(days=1)
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    return tuple(reversed(days))


def _history_points(quote: MarketQuote, position: int, days: tuple[date, ...]) -> tuple[PriceVolumePoint, ...]:
    if quote.previous_close is None or quote.previous_close <= 0:
        raise ValueError("performance_fixture_requires_previous_close")
    slope = 0.0002 + (position % 120) / 100_000.0
    last = len(days) - 1
    points: list[PriceVolumePoint] = []
    for index, day in enumerate(days):
        close = quote.previous_close * math.exp(
            slope * (index - last) + 0.001 * (math.sin(index / 7) - math.sin(last / 7))
        )
        volume = 10_000_000.0 + position * 100.0 + index * 1_000.0
        points.append(PriceVolumePoint(day, close * 0.999, close * 1.01, close * 0.99, close, volume, close * volume))
    return tuple(points)


def _model_feature(
    quote: MarketQuote,
    position: int,
    now: datetime,
    days: tuple[date, ...],
    profile: LoadedScoringProfile,
    history_sessions: int,
) -> FeatureSnapshot:
    points = _history_points(quote, position, days)
    horizons = (20, 40, 60, 120, 250) if profile.profile_id == "v2" else (20, 40, 60)
    anchors = QfqPriceAnchors(quote.price, tuple((horizon, points[-horizon].close) for horizon in (1, 3, 5, *horizons)))
    alpha = CalculateProfileQfqAlpha(anchors, horizons)
    if any(item.value is None for item in alpha):
        raise ValueError("performance_fixture_incomplete_alpha")
    average_amount = math.fsum(point.amount for point in points[-20:]) / 20.0
    amihud = (
        math.fsum(
            abs(current.close / previous.close - 1.0) / current.amount
            for previous, current in zip(points[-21:-1], points[-20:], strict=True)
        )
        / 20.0
    )
    values = {
        **_FEATURE_VALUES,
        **{item.feature_id.value: cast(float, item.value) for item in alpha},
        "qfq_average_amount_20d": average_amount,
        "qfq_amihud_20d": amihud,
    }
    if profile.profile_id == "v1":
        learned = CalculateLearnedRankFeatures(
            tuple(LearnedRankDailyPoint(point.trade_date, point.close, point.amount) for point in points)
        )
        if learned is None:
            raise ValueError("performance_fixture_incomplete_learned_rank")
        values.update(zip(LEARNED_RANK_FEATURE_IDS, learned, strict=True))
    sequence = (
        CalculatePriceVolumeSequence(
            quote.code,
            points[-history_sessions:],
            expected_dates=days[-history_sessions:],
            source_identity=_FIXTURE_SOURCE,
        )
        if profile.profile_id == "v5"
        else None
    )
    return FeatureSnapshot(
        quote,
        values,
        now,
        history_days=history_sessions,
        model_industry=ModelIndustryReference(
            quote.industry, "证监会行业分类", days[-1], "performance_fixture", _FIXTURE_SOURCE
        ),
        model_sequence=sequence,
        model_history_quality=ModelHistoryQuality(),
    )


def BuildWorstCaseCandidatePlans(market_rows: int, observed_at: datetime) -> CandidatePlanSet:
    population = tuple(_quote_input(index, observed_at) for index in range(market_rows))
    plans: dict[Strategy, ScoredCandidatePlan] = {}
    for strategy in SCORED_STRATEGIES:
        reserves = {
            board: tuple(item.code for item in population if item.board is board)
            for board in (Board.MAIN, Board.CHINEXT, Board.STAR)
        }
        plans[strategy] = ScoredCandidatePlan(
            evaluations=(),
            reserves=reserves,
            population_versions={},
            stage_counts=ScoredCandidateStageCounts(
                issuer_eligible_population=market_rows,
                input_ready_population=market_rows,
                dynamic_filter_eligible=market_rows,
                strategy_history_eligible=market_rows,
                model_input_eligible=market_rows,
                scoring_input_eligible=market_rows,
                scoring_input_count=market_rows,
            ),
            hard_filter_reason_counts={},
            population_rejected_count=0,
            population_filter_reason_counts={},
        )
    return CandidatePlanSet(plans, population_evaluated_at=observed_at)


def ProjectCandidateUnionQuotes(
    market_quotes: tuple[MarketQuote, ...],
    plans: CandidatePlanSet,
) -> tuple[MarketQuote, ...]:
    by_code = {quote.code: quote for quote in market_quotes}
    return tuple(by_code[code] for code in plans.physical_union())


_COMPLETE_REALTIME_FIELDS = (
    "name",
    "price",
    "previous_close",
    "open_price",
    "high",
    "low",
    "pct_change",
    "change_5m",
    "speed",
    "volume_ratio",
    "turnover_rate",
    "amount",
    "volume",
    "amplitude",
    "market_cap",
    "industry",
    "is_st",
    "is_suspended",
    "is_one_price_limit",
    "is_blacklisted",
    "has_major_regulatory_risk",
)


def CompleteRealtimeObservations(
    quotes: tuple[MarketQuote, ...],
    observed_at: datetime,
) -> tuple[SourceObservation, ...]:
    """Build the two admitted full-market provider batches used in production merge."""

    observations: list[SourceObservation] = []
    for quote in quotes:
        base = ObservationFromQuote(quote, source="tencent_market", observed_at=observed_at)
        fields = {name: base.fields[name] for name in _COMPLETE_REALTIME_FIELDS}
        for source in ("tencent_market", "sina"):
            data_version = f"{source}:{quote.data_version}"
            observations.append(
                replace(
                    base,
                    source=source,
                    data_version=data_version,
                    fields=fields,
                    payload_hash=hashlib.sha256(CanonicalJsonBytes(fields)).hexdigest(),
                )
            )
    return tuple(observations)


def _quote_input(index: int, now: datetime) -> MarketQuoteInput:
    if index < 2500:
        board, code = Board.MAIN, f"{600000 + index:06d}"
    elif index < 4000:
        board, code = Board.CHINEXT, f"{300000 + index - 2500:06d}"
    else:
        board, code = Board.STAR, f"{688000 + index - 4000:06d}"
    price = 10.0 + index / 10000.0
    high = max(10.2, price * 1.001)
    return MarketQuoteInput(
        code,
        f"样本{index}",
        price,
        10.0,
        10.0,
        high,
        9.8,
        (price / 10.0 - 1.0) * 100.0,
        0.1,
        0.1,
        1.2,
        2.0,
        price * 10_000_000.0,
        (high - 9.8) / 10.0 * 100.0,
        5_000_000_000.0,
        f"样本行业{index % 20}",
        "eastmoney",
        now,
        now,
        f"fixture:{index}",
        board=board,
        board_source="performance_fixture",
        board_reliability="verified",
        exchange="SZSE" if board is Board.CHINEXT else "SSE",
        listing_date=date(2020, 1, 1),
        volume=10_000_000.0,
    )


def _complete_quote(quote: MarketQuote, now: datetime) -> MarketQuote:
    return replace(
        quote,
        listing_age_sessions=1400,
        is_relisted_first_session=False,
        is_delisting_period_first_session=False,
        has_price_limit=True,
        exchange_limit_pct=10.0 if quote.board is Board.MAIN else 20.0,
        strategy_hot_cap_pct=8.0 if quote.board is Board.MAIN else 16.0,
        rule_version="performance-rule",
        rule_effective_date=now.date(),
    )


_FEATURE_VALUES = {
    "liquidity_score": 60.0,
    "short_momentum_score": 60.0,
    "trend_score": 70.0,
    "data_completeness_score": 100.0,
    "amount_median_20d": 200_000_000.0,
    "turnover_median_20d": 1.5,
    "return_1d": 2.0,
    "return_3d": 3.0,
    "return_5d": 5.0,
    "return_10d": 7.0,
    "return_20d": 10.0,
    "return_60d": 15.0,
    "volatility_20d": 2.0,
    "max_drawdown_20d": -8.0,
    "ma20_60_position": 70.0,
    "ma20_60_structure": 70.0,
    "ma_slope": 70.0,
    "breakout_20d": 70.0,
    "industry_trend": 70.0,
    "tail_return_30m": 1.0,
    "tail_volume_ratio": 1.2,
    "close_location": 70.0,
    "capacity_score": 100.0,
    "moderate_amplitude": 100.0,
    "price_executability": 100.0,
    "limit_distance_safety": 70.0,
    "quality_score": 70.0,
    "value_score": 70.0,
    "growth_score": 70.0,
    "atr20_pct": 2.0,
    "low_volatility_score": 70.0,
    "low_drawdown_score": 70.0,
    "market_breadth": 60.0,
    "market_regime_score": 50.0,
    "ma5": 12.0,
    "ma10": 11.8,
    "ma20": 11.5,
    "ma20_slope": 0.1,
    "high_20d_previous": 11.9,
}


__all__ = [
    "BuildPerformanceHistory",
    "PerformanceHistoryInput",
    "BuildPerformanceFixtures",
    "BuildWorstCaseCandidatePlans",
    "ProjectCandidateUnionQuotes",
    "CompleteRealtimeObservations",
]

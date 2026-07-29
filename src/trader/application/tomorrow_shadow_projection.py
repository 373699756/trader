"""Project existing point-in-time replay input into tomorrow v2 decisions."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from types import MappingProxyType
from typing import Literal
from zoneinfo import ZoneInfo

from trader.application.policy import RecommendationPolicy
from trader.application.ports.market import MarketDataPlaneSnapshot
from trader.application.ports.tomorrow import TomorrowNativeInput
from trader.application.recommendation_policy_codec import preselection_replay_feature
from trader.application.tomorrow_deepseek_fusion import (
    normalize_tomorrow_review_times,
    tomorrow_decision_policy,
)
from trader.application.tomorrow_selection import TomorrowSelectionOptions, select_tomorrow_snapshot
from trader.domain.market.epochs import (
    CANDIDATE_REALTIME_FEATURES,
    CandidateFeatureRow,
    CandidateQuoteEpoch,
    DailyFeaturePack,
    DailyFeatureRow,
    MarketEpoch,
)
from trader.domain.market.models import FeatureSnapshot, LiveQuote, MarketQuote
from trader.domain.recommendation.models import RecommendationSnapshot, Strategy
from trader.domain.recommendation.tomorrow_fusion import (
    DecisionEpoch,
    TomorrowDecisionRequest,
    build_tomorrow_decision_epoch,
    select_tomorrow_review_candidates,
)
from trader.domain.review.models import DeepSeekReview, ReviewOutcome

SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class TomorrowShadowProjection:
    input_version: str
    received_at: datetime
    local: DecisionEpoch
    hybrid: DecisionEpoch | None
    hard_filter_reason_counts: Mapping[str, int]

    @property
    def effective(self) -> DecisionEpoch:
        return self.hybrid or self.local


def project_tomorrow_snapshot(
    snapshot: RecommendationSnapshot,
    policy: RecommendationPolicy,
    *,
    decision_sequence: int,
) -> TomorrowShadowProjection:
    if snapshot.strategy is not Strategy.TOMORROW:
        raise ValueError("tomorrow shadow projection requires a tomorrow snapshot")
    replay = snapshot.replay_input
    if replay is None:
        raise ValueError("tomorrow shadow projection requires point-in-time replay input")
    return project_tomorrow_input(
        native_input_from_snapshot(snapshot),
        policy,
        decision_sequence=decision_sequence,
        reviews=replay.reviews,
    )


def project_tomorrow_input(
    native_input: TomorrowNativeInput,
    policy: RecommendationPolicy,
    *,
    decision_sequence: int,
    reviews: Mapping[str, DeepSeekReview] | None = None,
) -> TomorrowShadowProjection:
    if decision_sequence < 0:
        raise ValueError("tomorrow shadow decision sequence cannot be negative")
    evaluated_at = _shanghai(native_input.evaluated_at)
    plane = _data_plane_snapshot(native_input)
    selection = select_tomorrow_snapshot(
        plane,
        policy,
        TomorrowSelectionOptions(
            evaluated_at=evaluated_at,
            max_age_seconds=native_input.score_max_age_seconds,
            phase=native_input.phase,
            candidate_features=native_input.candidate_features,
            normalize_discovery_source_time=True,
        ),
    )
    market = plane.market
    if market is None:
        raise RuntimeError("tomorrow shadow market epoch is unavailable")
    decision_policy = tomorrow_decision_policy(policy)
    review_codes = tuple(item.code for item in select_tomorrow_review_candidates(selection, decision_policy))
    candidate_version = (
        plane.candidate_quotes.version
        if plane.candidate_quotes is not None
        and any(plane.candidate_quotes.version in item.features.merge_epoch for item in selection.evaluations)
        else None
    )
    local = build_tomorrow_decision_epoch(
        TomorrowDecisionRequest(
            selection=selection,
            reviews={},
            observed_at=evaluated_at,
            trade_date=market.trade_date,
            sequence=decision_sequence,
            config_version=market.config_version,
            strategy_version=policy.strategy_version,
            fusion_version=policy.fusion_version,
            market_epoch_version=market.version,
            candidate_epoch_version=candidate_version,
            research_epoch_version=None,
            projection_stage="local",
            parent_decision_version=None,
            review_candidate_codes=review_codes,
            degraded_reasons=(),
            policy=decision_policy,
        )
    )
    normalized = normalize_tomorrow_review_times(
        {code: review for code, review in (reviews or {}).items() if code in set(review_codes)},
        evaluated_at,
    )
    usable = _usable_reviews(normalized or {})
    hybrid = None
    if usable:
        observed_at = max(evaluated_at, *(review.completed_at for review in usable.values()))
        hybrid = build_tomorrow_decision_epoch(
            TomorrowDecisionRequest(
                selection=selection,
                reviews=normalized or {},
                observed_at=observed_at,
                trade_date=market.trade_date,
                sequence=decision_sequence + 1,
                config_version=market.config_version,
                strategy_version=policy.strategy_version,
                fusion_version=policy.fusion_version,
                market_epoch_version=market.version,
                candidate_epoch_version=candidate_version,
                research_epoch_version=None,
                projection_stage="hybrid",
                parent_decision_version=local.version,
                review_candidate_codes=review_codes,
                degraded_reasons=() if set(usable) == set(review_codes) else ("deepseek_incomplete",),
                policy=decision_policy,
            )
        )
    return TomorrowShadowProjection(
        input_version=native_input.input_version,
        received_at=market.received_at,
        local=local,
        hybrid=hybrid,
        hard_filter_reason_counts=selection.hard_filter_reason_counts,
    )


def native_input_from_snapshot(snapshot: RecommendationSnapshot) -> TomorrowNativeInput:
    if snapshot.strategy is not Strategy.TOMORROW:
        raise ValueError("tomorrow native input requires a tomorrow snapshot")
    replay = snapshot.replay_input
    if replay is None:
        raise ValueError("tomorrow native input requires point-in-time replay input")
    return TomorrowNativeInput(
        trade_date=date.fromisoformat(snapshot.trade_date),
        phase=snapshot.phase,
        data_version=snapshot.data_version,
        config_version=snapshot.config_version,
        evaluated_at=replay.evaluated_at,
        market_features=replay.market_features,
        requested_codes=replay.requested_codes,
        candidate_features=replay.candidate_features,
        preselect_max_age_seconds=replay.preselect_max_age_seconds,
        score_max_age_seconds=replay.score_max_age_seconds,
        candidate_pool_size=replay.candidate_pool_size,
    )


def _data_plane_snapshot(
    native_input: TomorrowNativeInput,
) -> MarketDataPlaneSnapshot:
    evaluated_at = native_input.evaluated_at
    market_features = _unique_features(
        tuple(preselection_replay_feature(feature) for feature in native_input.market_features)
    )
    candidate_features = _unique_features(native_input.candidate_features)
    if not market_features:
        raise ValueError("tomorrow shadow replay input has no market features")
    features_by_code = dict(market_features)
    trade_date = native_input.trade_date
    daily = DailyFeaturePack(
        trade_date=trade_date,
        sequence=0,
        observed_at=evaluated_at,
        received_at=evaluated_at,
        config_version=native_input.config_version,
        rows=tuple(_daily_row(feature, trade_date) for feature in features_by_code.values()),
        source_versions=_source_versions(tuple(market_features.values())),
    )
    quotes = tuple(_market_quote(feature.quote, evaluated_at) for feature in market_features.values())
    market_observed_at = max(quote.source_time for quote in quotes)
    market_received_at = max(market_observed_at, *(quote.received_time for quote in quotes))
    if market_received_at > evaluated_at:
        raise ValueError("tomorrow shadow replay contains quotes received after evaluation")
    market = MarketEpoch(
        trade_date=trade_date,
        sequence=0,
        observed_at=market_observed_at,
        received_at=market_received_at,
        config_version=native_input.config_version,
        daily_feature_pack_version=daily.version,
        quotes=quotes,
        source_versions=_source_versions(tuple(market_features.values())),
        market_regime=_market_regime(tuple(market_features.values())),
        degraded_reasons=(),
    )
    candidate = _candidate_epoch(
        tuple(candidate_features.values()),
        market,
        native_input.config_version,
        evaluated_at,
    )
    return MarketDataPlaneSnapshot(
        daily_features=daily,
        market=market,
        candidate_quotes=candidate,
        research=None,
    )


def _daily_row(feature: FeatureSnapshot, trade_date: date) -> DailyFeatureRow:
    values = {name: value for name, value in feature.values.items() if name not in CANDIDATE_REALTIME_FEATURES}
    missing_fields = tuple(name for name in feature.missing_fields if name not in CANDIDATE_REALTIME_FEATURES)
    missing_reasons = {
        name: reason for name, reason in feature.missing_reasons.items() if name not in CANDIDATE_REALTIME_FEATURES
    }
    return DailyFeatureRow(
        code=feature.quote.code,
        values=values,
        history_sessions=feature.history_days,
        data_as_of=trade_date - timedelta(days=1),
        missing_fields=missing_fields,
        missing_reasons=missing_reasons,
    )


def _candidate_epoch(
    features: tuple[FeatureSnapshot, ...],
    market: MarketEpoch,
    config_version: str,
    evaluated_at: datetime,
) -> CandidateQuoteEpoch | None:
    verified = tuple(
        feature
        for feature in features
        if feature.quote.cross_source_verified
        and feature.quote.cross_source_deviation_pct is not None
        and 0.0 <= feature.quote.cross_source_deviation_pct <= 0.5
    )
    if not verified:
        return None
    quotes = tuple(_live_quote(feature.quote, evaluated_at) for feature in verified)
    observed_at = max(quote.source_time for quote in quotes)
    received_at = max(observed_at, *(quote.received_time for quote in quotes))
    rows = tuple(_candidate_row(feature) for feature in verified)
    return CandidateQuoteEpoch(
        trade_date=market.trade_date,
        sequence=0,
        observed_at=observed_at,
        received_at=received_at,
        config_version=config_version,
        market_epoch_version=market.version,
        quotes=quotes,
        feature_rows=rows,
        source_versions=_source_versions(verified),
    )


def _candidate_row(feature: FeatureSnapshot) -> CandidateFeatureRow:
    values = {name: value for name, value in feature.values.items() if name in CANDIDATE_REALTIME_FEATURES}
    missing_fields = tuple(name for name in feature.missing_fields if name in CANDIDATE_REALTIME_FEATURES)
    missing_reasons = {
        name: reason for name, reason in feature.missing_reasons.items() if name in CANDIDATE_REALTIME_FEATURES
    }
    return CandidateFeatureRow(
        code=feature.quote.code,
        values=values,
        missing_fields=missing_fields,
        missing_reasons=missing_reasons,
    )


def _market_quote(quote: MarketQuote, evaluated_at: datetime) -> MarketQuote:
    normalized = replace(
        quote,
        source_time=_shanghai(quote.source_time),
        received_time=_shanghai(quote.received_time),
    )
    if normalized.source_time > evaluated_at or normalized.received_time > evaluated_at:
        raise ValueError("tomorrow shadow market quote is from the future")
    return normalized


def _live_quote(quote: MarketQuote, evaluated_at: datetime) -> LiveQuote:
    normalized = _market_quote(quote, evaluated_at)
    return LiveQuote(
        code=normalized.code,
        price=normalized.price,
        pct_change=normalized.pct_change,
        source=normalized.source,
        source_time=normalized.source_time,
        received_time=normalized.received_time,
        data_version=normalized.data_version,
        cross_source_deviation_pct=normalized.cross_source_deviation_pct,
        cross_source_verified=normalized.cross_source_verified,
    )


def _unique_features(features: Sequence[FeatureSnapshot]) -> dict[str, FeatureSnapshot]:
    result: dict[str, FeatureSnapshot] = {}
    for feature in sorted(features, key=lambda item: item.quote.code):
        if feature.quote.code in result:
            raise ValueError("tomorrow shadow replay feature codes must be unique")
        result[feature.quote.code] = feature
    return result


def _source_versions(features: tuple[FeatureSnapshot, ...]) -> Mapping[str, str]:
    versions: dict[str, str] = {}
    for feature in features:
        source = feature.quote.source.strip() or "unknown"
        versions[source] = max(versions.get(source, ""), feature.quote.data_version or "unknown")
    return MappingProxyType(dict(sorted(versions.items())))


def _market_regime(
    features: tuple[FeatureSnapshot, ...],
) -> Literal["risk_on", "neutral", "risk_off"]:
    counts = Counter(feature.market_regime for feature in features)
    regime = counts.most_common(1)[0][0] if counts else "neutral"
    if regime == "risk_on":
        return "risk_on"
    if regime == "risk_off":
        return "risk_off"
    return "neutral"


def _usable_reviews(reviews: Mapping[str, DeepSeekReview]) -> dict[str, DeepSeekReview]:
    return {
        code: review
        for code, review in reviews.items()
        if review.outcome in {ReviewOutcome.APPLIED, ReviewOutcome.ABSTAIN}
    }


def _shanghai(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("tomorrow shadow input time must be timezone-aware")
    return value.astimezone(SHANGHAI)


__all__ = [
    "TomorrowShadowProjection",
    "native_input_from_snapshot",
    "project_tomorrow_input",
    "project_tomorrow_snapshot",
]

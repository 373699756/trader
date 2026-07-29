"""Fixed-watchlist long quote projection without strategy scoring."""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from types import MappingProxyType
from typing import TYPE_CHECKING

from trader.application.cache import request_fingerprint
from trader.application.long_groups import LongGroupDefinition, LongWatchItemDefinition, long_groups_metadata
from trader.application.ports.market import MarketDataUnavailableError
from trader.application.schedule import MarketPhase, trade_date_at
from trader.application.snapshot_publication import admit_snapshot_to_p6
from trader.domain.market.models import Board, FeatureSnapshot, MarketQuote
from trader.domain.recommendation.models import (
    FusionMode,
    Recommendation,
    RecommendationAction,
    RecommendationSnapshot,
    ScoreBreakdown,
    Strategy,
)

if TYPE_CHECKING:
    from trader.application.pipeline import RecommendationPipeline

_LONG_STRATEGY_VERSION = "long_watchlist_current_quotes_v1"
_NOT_APPLICABLE_FUSION_VERSION = "not_applicable"
_MISSING_QUOTE_FIELDS = ("price", "pct_change", "amount", "turnover_rate", "market_cap")
_ZERO_SCORE = ScoreBreakdown(
    components={},
    base_score=0.0,
    local_risk_penalty=0.0,
    local_score=0.0,
    deepseek_score=None,
    confidence_coverage=0.0,
    deepseek_risk_penalty=0.0,
    final_score=0.0,
    fusion_mode=FusionMode.LOCAL_DEGRADED,
    fusion_applied=False,
)


@dataclass(frozen=True)
class LongProjectionRequest:
    previous: RecommendationSnapshot | None
    now: datetime
    phase: str
    trade_date: str
    data_version: str
    config_version: str


class LongQuoteProjectionService:
    """Build current long observations directly from fixed codes and quotes."""

    def __init__(
        self,
        *,
        codes: Sequence[str],
        items: Sequence[LongWatchItemDefinition] = (),
        groups: Sequence[LongGroupDefinition] = (),
    ) -> None:
        normalized_codes = tuple(dict.fromkeys(str(code).strip() for code in codes if str(code).strip()))
        item_by_code = {item.code: item for item in items}
        unknown_items = set(item_by_code) - set(normalized_codes)
        if unknown_items:
            raise ValueError("long watch items must belong to configured long codes")
        self._codes = normalized_codes
        self._items = MappingProxyType(dict(item_by_code))
        self._groups = tuple(groups)

    @property
    def codes(self) -> tuple[str, ...]:
        return self._codes

    def project(
        self,
        features: Sequence[FeatureSnapshot],
        request: LongProjectionRequest,
    ) -> RecommendationSnapshot:
        previous = request.previous
        now = request.now
        phase = request.phase
        trade_date = request.trade_date
        data_version = request.data_version
        config_version = request.config_version
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("long projection time must be timezone-aware")
        if not data_version or not config_version:
            raise ValueError("long projection versions must not be empty")
        fresh_by_code = _fresh_features(features, frozenset(self._codes), now)
        previous_by_code = _previous_features(previous, trade_date)
        selected_features: list[FeatureSnapshot] = []
        retained_count = 0
        missing_count = 0
        for code in self._codes:
            feature = fresh_by_code.get(code)
            if feature is None:
                feature = previous_by_code.get(code)
                if feature is not None:
                    retained_count += 1
                else:
                    feature = self._missing_feature(code, now, data_version)
                    missing_count += 1
            selected_features.append(self._apply_watch_identity(feature))

        recommendations = tuple(
            Recommendation(
                strategy=Strategy.LONG,
                features=feature,
                score=_ZERO_SCORE,
                local_risk_facts=(),
                deepseek_risk_facts=(),
                review=None,
                action=RecommendationAction.OBSERVE,
                action_reason="fixed_long_watchlist",
                veto=False,
                rank=index,
                target_price=None,
            )
            for index, feature in enumerate(selected_features, start=1)
        )
        degraded_reasons = ("long_quotes_partial",) if len(fresh_by_code) != len(self._codes) else ()
        identity = request_fingerprint(
            {
                "strategy": Strategy.LONG.value,
                "trade_date": trade_date,
                "phase": phase,
                "data_version": data_version,
                "published_at": now,
                "quotes": tuple(
                    (
                        item.features.quote.code,
                        item.features.quote.data_version,
                        item.features.quote.source_time,
                        item.features.quote.price,
                        item.features.quote.pct_change,
                    )
                    for item in recommendations
                ),
            }
        )
        metadata: dict[str, object] = {
            "projection_stage": "current_quotes",
            "score_status": "not_applicable",
            "candidate_count": len(recommendations),
            "reviewed_count": 0,
            "quote_covered_count": len(fresh_by_code),
            "quote_retained_count": retained_count,
            "quote_missing_count": missing_count,
            "selection_diagnostics": {
                "scored_candidate_count": 0,
                "actionable_candidate_count": 0,
                "score_qualified_count": 0,
                "selection_floor": None,
                "maximum_local_score": None,
                "maximum_final_score": None,
                "empty_reason": "scoring_not_applicable",
            },
            "long_groups": long_groups_metadata(self._groups, recommendations),
        }
        if phase == "close_fallback":
            metadata.update({"recovery_path": "after_close_current", "price_basis": "official_close"})
        return RecommendationSnapshot(
            snapshot_id=f"long:{trade_date}:{identity[:24]}",
            strategy=Strategy.LONG,
            trade_date=trade_date,
            phase=phase,
            data_version=data_version,
            strategy_version=_LONG_STRATEGY_VERSION,
            fusion_version=_NOT_APPLICABLE_FUSION_VERSION,
            fusion_mode=FusionMode.LOCAL_DEGRADED,
            published_at=now,
            recommendations=recommendations,
            filtered_count=0,
            filter_reasons={},
            config_version=config_version,
            stale=any(
                item.features.quote.price is not None and item.features.quote.age_seconds(now) > 30.0
                for item in recommendations
            ),
            frozen=False,
            degraded_reasons=degraded_reasons,
            metadata=metadata,
            replay_input=None,
        )

    def _apply_watch_identity(self, feature: FeatureSnapshot) -> FeatureSnapshot:
        feature = _quote_only_feature(feature)
        item = self._items.get(feature.quote.code)
        if item is None:
            return feature
        quote = replace(feature.quote, name=item.name, industry=item.industry)
        return replace(feature, quote=quote)

    def _missing_feature(self, code: str, now: datetime, data_version: str) -> FeatureSnapshot:
        item = self._items.get(code)
        return FeatureSnapshot(
            quote=MarketQuote(
                code=code,
                name=item.name if item is not None else code,
                price=None,
                previous_close=None,
                open_price=None,
                high=None,
                low=None,
                pct_change=None,
                change_5m=None,
                speed=None,
                volume_ratio=None,
                turnover_rate=None,
                amount=None,
                amplitude=None,
                market_cap=None,
                industry=item.industry if item is not None else "",
                source="long_watchlist",
                source_time=now,
                received_time=now,
                data_version=f"{data_version}:missing",
                board=Board.UNSUPPORTED,
            ),
            values={},
            observed_at=now,
            missing_fields=_MISSING_QUOTE_FIELDS,
            missing_reasons={"quote": "long_watchlist_quote_unavailable"},
        )


def refresh_long_quotes(
    pipeline: RecommendationPipeline,
    now: datetime,
    phase: MarketPhase,
    *,
    deadline: datetime | None = None,
) -> tuple[RecommendationSnapshot, ...]:
    """Fetch and publish one current long observation without scoring."""

    codes = pipeline._long_projection.codes
    if not codes:
        return ()
    started = time.perf_counter()
    trade_date = trade_date_at(now).isoformat()
    previous = pipeline._state.latest(Strategy.LONG)
    try:
        refresh = getattr(pipeline._quotes, "refresh_long_quotes", None)
        if not callable(refresh):
            refresh = pipeline._quotes.refresh_candidate_quotes
        features = tuple(
            refresh(
                codes,
                now,
                force=phase in {MarketPhase.FINAL_QUOTE, MarketPhase.AFTER_CLOSE},
                deadline=deadline,
            )
        )
    except (MarketDataUnavailableError, OSError, RuntimeError, TypeError, ValueError) as exc:
        pipeline._state.increment("long_quote_failures")
        pipeline._state.record_strategy_degraded(Strategy.LONG, ("long_quote_unavailable",))
        pipeline._state.record_error(f"long quote lane degraded: {str(exc)[:400]}")
        if previous is not None and previous.trade_date == trade_date:
            return ()
        features = ()
    data_version = _long_data_version(features, now)
    snapshot = pipeline._long_projection.project(
        features,
        LongProjectionRequest(
            previous,
            now,
            "close_fallback" if phase is MarketPhase.AFTER_CLOSE else phase.value,
            trade_date,
            data_version,
            pipeline._config_version,
        ),
    )
    if not admit_snapshot_to_p6(pipeline, snapshot):
        return ()
    pipeline._state.publish(snapshot)
    pipeline._state.record_strategy_latency(Strategy.LONG, round((time.perf_counter() - started) * 1000.0, 3))
    pipeline._session_snapshot_ids.add(snapshot.snapshot_id)
    pipeline._publisher.publish(snapshot)
    pipeline._state.increment("long_quote_snapshots_published")
    return (snapshot,)


def _previous_features(
    previous: RecommendationSnapshot | None,
    trade_date: str,
) -> dict[str, FeatureSnapshot]:
    if previous is None or previous.trade_date != trade_date:
        return {}
    return {item.features.quote.code: item.features for item in previous.recommendations}


def _fresh_features(
    features: Sequence[FeatureSnapshot],
    allowed_codes: frozenset[str],
    now: datetime,
) -> dict[str, FeatureSnapshot]:
    selected: dict[str, FeatureSnapshot] = {}
    for feature in features:
        quote = feature.quote
        if quote.code not in allowed_codes or quote.source_time > now or quote.price is None or quote.price <= 0:
            continue
        current = selected.get(quote.code)
        if current is None or (
            quote.source_time,
            quote.received_time,
            quote.data_version,
        ) > (
            current.quote.source_time,
            current.quote.received_time,
            current.quote.data_version,
        ):
            selected[quote.code] = feature
    return selected


def _quote_only_feature(feature: FeatureSnapshot) -> FeatureSnapshot:
    return FeatureSnapshot(
        quote=feature.quote,
        values={},
        observed_at=feature.observed_at,
        missing_fields=tuple(field for field in _MISSING_QUOTE_FIELDS if getattr(feature.quote, field) is None),
        missing_reasons={},
    )


def _long_data_version(features: Sequence[FeatureSnapshot], now: datetime) -> str:
    fingerprint = request_fingerprint(
        {
            "observed_at": now,
            "quotes": tuple(
                (
                    feature.quote.code,
                    feature.quote.data_version,
                    feature.quote.source_time,
                    feature.quote.price,
                    feature.quote.pct_change,
                )
                for feature in sorted(features, key=lambda item: item.quote.code)
            ),
        }
    )
    return f"long-quotes:{fingerprint[:24]}"


__all__ = ["LongProjectionRequest", "LongQuoteProjectionService", "refresh_long_quotes"]

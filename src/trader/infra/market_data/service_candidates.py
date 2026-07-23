"""Candidate quote selection, enrichment and action restrictions."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from typing_extensions import Unpack

from trader.domain.market.models import (
    FeatureSnapshot,
    LiveQuote,
    MarketQuote,
)
from trader.domain.market.research import ResearchObservation
from trader.domain.market.tail import MinuteBar
from trader.infra.market_data.features import StandardizedFeatureBuilder
from trader.infra.market_data.gateway import MarketDataGateway
from trader.infra.market_data.history import DailyBar
from trader.infra.market_data.service_history import HistoryStore
from trader.infra.market_data.service_support import _quote_version
from trader.infra.market_data.service_tushare import ReferenceLoader

_AUXILIARY_ACTION_RESTRICTIONS = frozenset(
    {"history_data_degraded", "intraday_data_degraded", "research_data_degraded"}
)


@dataclass(frozen=True)
class QuoteStoreStatus:
    market_features: tuple[FeatureSnapshot, ...]
    candidate_quotes: tuple[MarketQuote, ...]
    market_feature_rows: int
    candidate_quote_entries: int
    out_of_order_count: int


@dataclass(frozen=True)
class QuoteStoreDependencies:
    gateway: MarketDataGateway
    feature_builder: StandardizedFeatureBuilder
    history: HistoryStore
    references: ReferenceLoader


class _CandidateFeatureRequiredOptions(TypedDict):
    research_observations: Mapping[str, ResearchObservation]
    intraday_minutes: Mapping[str, Sequence[MinuteBar]] | None


class _CandidateFeatureOptionalOptions(TypedDict, total=False):
    action_restrictions: Mapping[str, set[str]] | None


class _CandidateFeatureOptions(_CandidateFeatureRequiredOptions, _CandidateFeatureOptionalOptions):
    pass


class QuoteStore:
    def __init__(
        self,
        dependencies: QuoteStoreDependencies,
        *,
        market_ttl_seconds: float,
        candidate_capacity: int,
        monotonic: Callable[[], float],
    ) -> None:
        self.gateway = dependencies.gateway
        self._feature_builder = dependencies.feature_builder
        self._history = dependencies.history
        self._references = dependencies.references
        self._market_ttl_seconds = max(1.0, market_ttl_seconds)
        self._candidate_capacity = max(1, candidate_capacity)
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._market_features: tuple[FeatureSnapshot, ...] = ()
        self._market_expires_at = 0.0
        self._candidate_quotes: dict[str, MarketQuote] = {}
        self._out_of_order_count = 0

    def candidate_snapshot(self, codes: Sequence[str]) -> tuple[MarketQuote, ...]:
        with self._lock:
            market = {feature.quote.code: feature.quote for feature in self._market_features}
            result: list[MarketQuote] = []
            for code in codes:
                targeted = self._candidate_quotes.get(code)
                full_market = market.get(code)
                available = tuple(quote for quote in (targeted, full_market) if quote is not None)
                if available:
                    result.append(max(available, key=_quote_version))
            return tuple(result)

    def build_candidate_features(
        self,
        quotes: Sequence[MarketQuote],
        histories: Mapping[str, tuple[DailyBar, ...]],
        observed_at: datetime,
        **options: Unpack[_CandidateFeatureOptions],
    ) -> tuple[FeatureSnapshot, ...]:
        research_observations = options["research_observations"]
        intraday_minutes = options["intraday_minutes"]
        action_restrictions = options.get("action_restrictions")
        with self._lock:
            cross_section_reference = {feature.quote.code: feature.values for feature in self._market_features}
            cross_section_normalization_reference = {
                feature.quote.code: feature.normalization for feature in self._market_features
            }
        history_summaries = self._history.summaries(histories, observed_at)
        features = self._feature_builder.build(
            quotes,
            histories,
            observed_at,
            cross_section_reference=cross_section_reference,
            cross_section_normalization_reference=cross_section_normalization_reference,
            research_observations=research_observations,
            intraday_minutes=intraday_minutes,
            history_summaries=history_summaries,
        )
        feature_codes = {feature.quote.code for feature in features}
        tushare_fields = self._references.fields(tuple(feature_codes))
        enriched = tuple(
            replace(feature, values={**feature.values, **tushare_fields.get(feature.quote.code, {})})
            for feature in features
        )
        return _apply_action_restrictions(enriched, action_restrictions or {})

    def build_market_features(
        self,
        quotes: Sequence[MarketQuote],
        histories: Mapping[str, tuple[DailyBar, ...]],
        observed_at: datetime,
        *,
        action_restrictions: Mapping[str, set[str]],
    ) -> tuple[FeatureSnapshot, ...]:
        return _apply_action_restrictions(
            self._feature_builder.build(
                quotes,
                histories,
                observed_at,
                history_summaries=self._history.summaries(histories, observed_at),
            ),
            action_restrictions,
        )

    def cached_market_features(self, *, force: bool) -> tuple[FeatureSnapshot, ...] | None:
        with self._lock:
            if not force and self._market_features and self._market_expires_at > self._monotonic():
                return self._market_features
        return None

    def publish_market_features(self, features: Sequence[FeatureSnapshot]) -> tuple[FeatureSnapshot, ...]:
        published = tuple(features)
        with self._lock:
            self._market_features = published
            self._market_expires_at = self._monotonic() + self._market_ttl_seconds
        return published

    def market_quotes(self) -> tuple[MarketQuote, ...]:
        with self._lock:
            return tuple(feature.quote for feature in self._market_features)

    def update_candidate_quotes(self, quotes: Sequence[MarketQuote]) -> None:
        with self._lock:
            market_quotes = {feature.quote.code: feature.quote for feature in self._market_features}
            for quote in quotes:
                available = tuple(
                    item
                    for item in (self._candidate_quotes.get(quote.code), market_quotes.get(quote.code))
                    if item is not None
                )
                current = max(available, key=_quote_version) if available else None
                if current is not None and _quote_version(quote) < _quote_version(current):
                    self._out_of_order_count += 1
                    continue
                self._candidate_quotes[quote.code] = quote
            excess = len(self._candidate_quotes) - self._candidate_capacity
            if excess > 0:
                for code in sorted(
                    self._candidate_quotes,
                    key=lambda item: (_quote_version(self._candidate_quotes[item]), item),
                )[:excess]:
                    self._candidate_quotes.pop(code, None)

    def current_quotes(self, codes: Sequence[str]) -> Mapping[str, LiveQuote]:
        resolved = {quote.code: quote for quote in self.candidate_snapshot(codes)}
        for quote in self.gateway.current_quotes(codes):
            current = resolved.get(quote.code)
            if current is None or _quote_version(quote) > _quote_version(current):
                resolved[quote.code] = quote
        projected: dict[str, LiveQuote] = {}
        for code in codes:
            selected = resolved.get(code)
            if selected is None:
                continue
            projected[selected.code] = LiveQuote(
                code=selected.code,
                price=selected.price,
                pct_change=selected.pct_change,
                source=selected.source,
                source_time=selected.source_time,
                received_time=selected.received_time,
                data_version=selected.data_version,
            )
        return projected

    def status(self) -> QuoteStoreStatus:
        with self._lock:
            return QuoteStoreStatus(
                market_features=self._market_features,
                candidate_quotes=tuple(self._candidate_quotes.values()),
                market_feature_rows=len(self._market_features),
                candidate_quote_entries=len(self._candidate_quotes),
                out_of_order_count=self._out_of_order_count,
            )

    def candidate_entries(self) -> Mapping[str, MarketQuote]:
        with self._lock:
            return dict(self._candidate_quotes)


def _apply_action_restrictions(
    features: Sequence[FeatureSnapshot],
    action_restrictions: Mapping[str, set[str]],
) -> tuple[FeatureSnapshot, ...]:
    return tuple(
        replace(
            feature,
            quote=replace(
                feature.quote,
                execution_restrictions=tuple(
                    sorted(
                        {
                            *(
                                reason
                                for reason in feature.quote.execution_restrictions
                                if reason not in _AUXILIARY_ACTION_RESTRICTIONS
                            ),
                            *action_restrictions.get(feature.quote.code, set()),
                        }
                    )
                ),
            ),
        )
        for feature in features
    )


__all__ = ["QuoteStore", "QuoteStoreStatus", "_apply_action_restrictions"]

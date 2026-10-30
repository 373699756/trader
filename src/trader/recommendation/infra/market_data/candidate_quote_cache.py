"""Candidate quote selection, enrichment and action restrictions."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from typing_extensions import Unpack

from trader.infra.market_data.history.history import DailyBar
from trader.recommendation.application.runtime.schedule import ShanghaiNow
from trader.recommendation.domain.market.history_alignment import ModelHistoryQuality
from trader.recommendation.domain.market.industry import QuoteWithIndustry
from trader.recommendation.domain.market.models import (
    CrossSectionStats,
    FeatureSnapshot,
    LiveQuote,
    MarketQuote,
)
from trader.recommendation.domain.market.research import ResearchObservation
from trader.recommendation.domain.market.tail import MinuteBar
from trader.recommendation.domain.market.v5.features import ModelPriceVolumeSequence
from trader.recommendation.domain.publication.preview import PreviewContext
from trader.recommendation.infra.market_data.gateway import MarketDataGateway
from trader.recommendation.infra.market_data.market_cache_identity import _quote_version
from trader.recommendation.infra.market_data.published_history_cache import PublishedHistoryCache
from trader.recommendation.infra.market_data.published_references import PublishedReferences
from trader.recommendation.infra.normalization.features import StandardizedFeatureBuilder

_AUXILIARY_ACTION_RESTRICTIONS = frozenset(
    {"history_data_degraded", "intraday_data_degraded", "research_data_degraded"}
)


@dataclass(frozen=True)
class QuoteCacheStatus:
    market_features: tuple[FeatureSnapshot, ...]
    selected_candidate_quotes: tuple[MarketQuote, ...]
    market_feature_rows: int
    candidate_quote_entries: int
    out_of_order_count: int


@dataclass(frozen=True)
class QuoteCacheDependencies:
    gateway: MarketDataGateway
    feature_builder: StandardizedFeatureBuilder
    history: PublishedHistoryCache
    references: PublishedReferences


class _CandidateFeatureRequiredOptions(TypedDict):
    research_observations: Mapping[str, ResearchObservation]
    intraday_minutes: Mapping[str, Sequence[MinuteBar]] | None


class _CandidateFeatureOptionalOptions(TypedDict, total=False):
    action_restrictions: Mapping[str, set[str]] | None


class _CandidateFeatureOptions(_CandidateFeatureRequiredOptions, _CandidateFeatureOptionalOptions):
    pass


class QuoteCache:
    def __init__(
        self,
        dependencies: QuoteCacheDependencies,
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
        self._market_quotes_by_code: Mapping[str, MarketQuote] = MappingProxyType({})
        self._cross_section_reference: Mapping[str, Mapping[str, float | None]] = MappingProxyType({})
        self._cross_section_normalization_reference: Mapping[str, Mapping[str, CrossSectionStats]] = MappingProxyType(
            {}
        )
        self._market_expires_at = 0.0
        self._market_reference_epoch = ""
        self._candidate_quotes: dict[str, MarketQuote] = {}
        self._candidate_cycle: datetime | None = None
        self._selected_candidate_codes: set[str] = set()
        self._out_of_order_count = 0
        self._preview_context: PreviewContext | None = None

    def prepare_preview(self, context: PreviewContext | None) -> None:
        with self._lock:
            if context != self._preview_context:
                self._market_features = ()
                self._market_quotes_by_code = MappingProxyType({})
                self._cross_section_reference = MappingProxyType({})
                self._cross_section_normalization_reference = MappingProxyType({})
                self._candidate_quotes.clear()
                self._selected_candidate_codes.clear()
                self._candidate_cycle = None
                self._preview_context = context
        self.gateway.prepare_preview(context)

    def candidate_snapshot(self, codes: Sequence[str]) -> tuple[MarketQuote, ...]:
        with self._lock:
            result: list[MarketQuote] = []
            for code in codes:
                targeted = self._candidate_quotes.get(code)
                full_market = self._market_quotes_by_code.get(code)
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
            cross_section_reference = self._cross_section_reference
            cross_section_normalization_reference = self._cross_section_normalization_reference
        history_summaries = self._history.summaries(histories, observed_at)
        local_quotes = self._with_industry_quotes(quotes, observed_at)
        features = self._feature_builder.build(
            local_quotes,
            histories,
            observed_at,
            cross_section_reference=cross_section_reference,
            cross_section_normalization_reference=cross_section_normalization_reference,
            research_observations=research_observations,
            intraday_minutes=intraday_minutes,
            history_summaries=history_summaries,
            preview_context=self._preview_context,
        )
        return self._with_model_industries(features, action_restrictions=action_restrictions or {})

    def with_intraday_features(
        self,
        features: tuple[FeatureSnapshot, ...],
        minutes: Mapping[str, Sequence[MinuteBar]],
        observed_at: datetime,
        restrictions: Mapping[str, set[str]],
    ) -> tuple[FeatureSnapshot, ...]:
        enriched = self._feature_builder.with_intraday(features, minutes, observed_at)
        return tuple(
            replace(feature, quote=replace(
                feature.quote,
                execution_restrictions=tuple(sorted({
                    *(item for item in feature.quote.execution_restrictions if item != "intraday_data_degraded"),
                    *restrictions.get(feature.quote.code, set()),
                })),
            )) if restrictions.get(feature.quote.code) or "intraday_data_degraded" in feature.quote.execution_restrictions
            else feature
            for feature in enriched
        )

    def build_market_features(
        self,
        quotes: Sequence[MarketQuote],
        histories: Mapping[str, tuple[DailyBar, ...]],
        observed_at: datetime,
        *,
        research_observations: Mapping[str, ResearchObservation] | None = None,
        action_restrictions: Mapping[str, set[str]],
    ) -> tuple[FeatureSnapshot, ...]:
        started = self._monotonic()
        summaries = self._history.summaries(histories, observed_at)
        local_quotes = self._with_industry_quotes(quotes, observed_at)
        self.gateway.record_local_latency("market_history_summary", (self._monotonic() - started) * 1000)
        started = self._monotonic()
        features = self._feature_builder.build(
            local_quotes,
            histories,
            observed_at,
            research_observations=research_observations,
            history_summaries=summaries,
            preview_context=self._preview_context,
        )
        self.gateway.record_local_latency("market_factor_build", (self._monotonic() - started) * 1000)
        started = self._monotonic()
        features = self._with_model_industries(features, action_restrictions=action_restrictions)
        self.gateway.record_local_latency("market_model_inputs", (self._monotonic() - started) * 1000)
        return features

    def cached_market_features(self, *, force: bool, reference_epoch: str) -> tuple[FeatureSnapshot, ...] | None:
        with self._lock:
            if (
                not force
                and self._market_features
                and self._market_expires_at > self._monotonic()
                and self._market_reference_epoch == reference_epoch
            ):
                cached = self._market_features
            else:
                cached = None
        if cached is not None:
            return self._with_model_industries(cached)
        return None

    def _with_industry_quotes(self, quotes: Sequence[MarketQuote], observed_at: datetime) -> tuple[MarketQuote, ...]:
        references = self._references.model_industries(tuple(quote.code for quote in quotes))
        day = self._preview_context.market_trade_date if self._preview_context else ShanghaiNow(observed_at).date()
        return tuple(QuoteWithIndustry(quote, references.get(quote.code), day) for quote in quotes)

    def _with_model_industries(
        self,
        features: Sequence[FeatureSnapshot],
        *,
        action_restrictions: Mapping[str, set[str]] | None = None,
    ) -> tuple[FeatureSnapshot, ...]:
        references = self._references.model_industries(tuple(feature.quote.code for feature in features))
        by_time: dict[datetime, list[str]] = {}
        for feature in features:
            by_time.setdefault(feature.observed_at, []).append(feature.quote.code)
        completed: dict[str, tuple[tuple[str, float], ...]] = {}
        sequences: dict[str, ModelPriceVolumeSequence] = {}
        quality: dict[str, ModelHistoryQuality] = {}
        for observed_at, codes in by_time.items():
            for item in self._history.completed_model_inputs(codes, observed_at):
                completed[item.code] = item.learned_rank_values
                quality[item.code] = item.quality
                if item.sequence is not None:
                    sequences[item.code] = item.sequence
        result: list[FeatureSnapshot] = []
        for feature in features:
            code = feature.quote.code
            day = self._preview_context.market_trade_date if self._preview_context else ShanghaiNow(feature.observed_at).date()
            reference = references.get(code)
            quote = QuoteWithIndustry(feature.quote, reference, day)
            if action_restrictions is not None:
                quote = _restricted_quote(quote, action_restrictions.get(code, set()))
            learned = completed.get(code, ())
            result.append(replace(
                feature,
                values={**feature.values, **dict(learned)} if learned else feature.values,
                model_sequence=sequences.get(code),
                model_history_quality=quality[code],
                quote=quote,
                model_industry=reference if reference is not None and reference.effective_date <= day else None,
            ))
        return tuple(result)

    def publish_market_features(
        self, features: Sequence[FeatureSnapshot], *, reference_epoch: str
    ) -> tuple[FeatureSnapshot, ...]:
        published = tuple(features)
        with self._lock:
            self._market_features = published
            self._market_quotes_by_code = MappingProxyType({feature.quote.code: feature.quote for feature in published})
            self._cross_section_reference = MappingProxyType(
                {feature.quote.code: feature.values for feature in published}
            )
            self._cross_section_normalization_reference = MappingProxyType(
                {feature.quote.code: feature.normalization for feature in published}
            )
            self._market_reference_epoch = reference_epoch
            self._market_expires_at = self._monotonic() + self._market_ttl_seconds
        return published

    def market_features(self) -> tuple[FeatureSnapshot, ...]:
        with self._lock:
            return self._market_features

    def update_candidate_quotes(
        self,
        quotes: Sequence[MarketQuote],
        *,
        candidate_cycle: datetime | None = None,
    ) -> None:
        with self._lock:
            if candidate_cycle is not None and (
                self._candidate_cycle is None or candidate_cycle > self._candidate_cycle
            ):
                self._candidate_cycle = candidate_cycle
                self._selected_candidate_codes.clear()
            for quote in quotes:
                available = tuple(
                    item
                    for item in (self._candidate_quotes.get(quote.code), self._market_quotes_by_code.get(quote.code))
                    if item is not None
                )
                current = max(available, key=_quote_version) if available else None
                if current is not None and _quote_version(quote) < _quote_version(current):
                    self._out_of_order_count += 1
                    continue
                self._candidate_quotes[quote.code] = quote
                if candidate_cycle is not None and candidate_cycle == self._candidate_cycle:
                    self._selected_candidate_codes.add(quote.code)
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

    def status(self) -> QuoteCacheStatus:
        with self._lock:
            return QuoteCacheStatus(
                market_features=self._market_features,
                selected_candidate_quotes=tuple(
                    self._candidate_quotes[code]
                    for code in sorted(self._selected_candidate_codes)
                    if code in self._candidate_quotes
                ),
                market_feature_rows=len(self._market_features),
                candidate_quote_entries=len(self._candidate_quotes),
                out_of_order_count=self._out_of_order_count,
            )

    def candidate_entries(self) -> Mapping[str, MarketQuote]:
        with self._lock:
            return dict(self._candidate_quotes)


def _restricted_quote(quote: MarketQuote, restrictions: set[str]) -> MarketQuote:
    reasons = tuple(sorted({
        *(reason for reason in quote.execution_restrictions if reason not in _AUXILIARY_ACTION_RESTRICTIONS),
        *restrictions,
    }))
    return quote if reasons == quote.execution_restrictions else replace(quote, execution_restrictions=reasons)


__all__ = ["QuoteCache", "QuoteCacheStatus"]

"""Candidate quote selection, enrichment and action restrictions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime

from trader.domain.models import FeatureSnapshot, MarketQuote
from trader.domain.research import ResearchObservation
from trader.domain.tail import MinuteBar
from trader.infrastructure.market_data.history import DailyBar
from trader.infrastructure.market_data.service_state import MarketServiceState
from trader.infrastructure.market_data.service_support import _quote_version

_AUXILIARY_ACTION_RESTRICTIONS = frozenset(
    {"history_data_degraded", "intraday_data_degraded", "research_data_degraded"}
)


class MarketCandidateMixin(MarketServiceState):
    def _candidate_quote_snapshot(self, codes: Sequence[str]) -> tuple[MarketQuote, ...]:
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

    def _build_candidate_features(
        self,
        quotes: Sequence[MarketQuote],
        histories: Mapping[str, tuple[DailyBar, ...]],
        observed_at: datetime,
        *,
        research_observations: Mapping[str, ResearchObservation],
        intraday_minutes: Mapping[str, Sequence[MinuteBar]] | None,
        action_restrictions: Mapping[str, set[str]] | None = None,
    ) -> tuple[FeatureSnapshot, ...]:
        with self._lock:
            cross_section_reference = {feature.quote.code: feature.values for feature in self._market_features}
            cross_section_normalization_reference = {
                feature.quote.code: feature.normalization for feature in self._market_features
            }
        features = self._feature_builder.build(
            quotes,
            histories,
            observed_at,
            cross_section_reference=cross_section_reference,
            cross_section_normalization_reference=cross_section_normalization_reference,
            research_observations=research_observations,
            intraday_minutes=intraday_minutes,
        )
        feature_codes = {feature.quote.code for feature in features}
        with self._lock:
            tushare_fields = {
                code: dict(values) for code, values in self._tushare_reference_fields.items() if code in feature_codes
            }
        enriched = tuple(
            replace(feature, values={**feature.values, **tushare_fields.get(feature.quote.code, {})})
            for feature in features
        )
        return _apply_action_restrictions(enriched, action_restrictions or {})


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


__all__ = ["MarketCandidateMixin"]

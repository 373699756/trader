"""Explicit replay candidate for repeated raw factors, using the existing cache budget."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import cast
from zoneinfo import ZoneInfo

from trader.infra.cache_contracts import CacheIdentity, RequestKey
from trader.recommendation.domain.market.history_metrics import DailyBar, HistoryContext
from trader.infra.serialization.canonical import ContentHash
from trader.recommendation.domain.market.models import MarketQuote
from trader.recommendation.domain.market.feature_schema import FEATURE_SCHEMA_ID
from trader.recommendation.infra.normalization.features import FeatureBuilder

RAW_FACTOR_REUSE_CANDIDATE = "raw_factor_reuse"
_SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class _RawFactors:
    values: tuple[tuple[str, float | None], ...]


class RawFactorReuseBuilder(FeatureBuilder):
    """Memoize raw results only; all later observations and model boundaries stay live."""

    def _raw_features(
        self,
        quote: MarketQuote,
        bars: tuple[DailyBar, ...],
        history_summary: HistoryContext | None = None,
    ) -> dict[str, float | None]:
        calculate: Callable[
            [MarketQuote, tuple[DailyBar, ...], HistoryContext | None], dict[str, float | None]
        ] = super()._raw_features
        cache = self._cache
        if cache is None:
            return calculate(quote, bars, history_summary)
        try:
            inputs = ContentHash((quote, bars, history_summary, self._model_momentum_horizons))
        except ValueError:
            # Canonical identities reject nonfinite numbers; preserve the original
            # missing-value calculation without memoization for those inputs.
            return calculate(quote, bars, history_summary)
        identity = CacheIdentity(
            "quote_factor_inputs",
            RAW_FACTOR_REUSE_CANDIDATE,
            quote.code,
            RequestKey({"inputs": inputs}),
            quote.source_time.astimezone(_SHANGHAI).date().isoformat(),
            "quote_factors",
            "qfq",
            self._base_policy_identity,
            FEATURE_SCHEMA_ID,
        )
        found = cache.get(identity)
        if found is not None and found.value is not None:
            return dict(cast(_RawFactors, found.value).values)

        def build() -> _RawFactors:
            found = cache.get(identity)
            if found is not None and found.value is not None:
                return cast(_RawFactors, found.value)
            result = _RawFactors(tuple(calculate(quote, bars, history_summary).items()))
            cache.put(
                identity,
                result,
                data_version=quote.data_version,
                source_time=quote.source_time,
                received_at=quote.received_time,
            )
            return result

        return dict(cast(_RawFactors, cache.coalesce(identity, build)).values)


__all__ = [
    "RAW_FACTOR_REUSE_CANDIDATE",
    "RawFactorReuseBuilder",
]

"""Read-only current-quote projection for recommendation queries."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from trader.domain.models import LiveQuote
from trader.infra.market_data.service_state import MarketServiceState
from trader.infra.market_data.service_support import _normalize_codes, _quote_version


class MarketCurrentQuoteMixin(MarketServiceState):
    def current_quotes(self, codes: Sequence[str]) -> Mapping[str, LiveQuote]:
        normalized = _normalize_codes(codes)
        resolved = {quote.code: quote for quote in self._candidate_quote_snapshot(normalized)}
        for quote in self._gateway.current_quotes(normalized):
            current = resolved.get(quote.code)
            if current is None or _quote_version(quote) > _quote_version(current):
                resolved[quote.code] = quote
        return {
            quote.code: LiveQuote(
                code=quote.code,
                price=quote.price,
                pct_change=quote.pct_change,
                source=quote.source,
                source_time=quote.source_time,
                received_time=quote.received_time,
                data_version=quote.data_version,
            )
            for code in normalized
            if (quote := resolved.get(code)) is not None
        }

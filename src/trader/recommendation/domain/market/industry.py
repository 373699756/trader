"""Point-in-time industry projection shared by market and candidate features."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

from trader.recommendation.domain.market.models import MarketQuote, ModelIndustryReference


def QuoteWithIndustry(quote: MarketQuote, reference: ModelIndustryReference | None, as_of: date) -> MarketQuote:
    industry = reference.display_name if reference is not None and reference.effective_date <= as_of else ""
    return quote if quote.industry == industry else replace(quote, industry=industry)


__all__ = ["QuoteWithIndustry"]

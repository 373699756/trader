from __future__ import annotations

from typing import Dict, Tuple

import pandas as pd

from . import config
from .app_support import (
    attach_alphalite_factors,
    attach_alphalite_factors_for_codes,
    sentiment_for_candidates,
)
from .event_risk import attach_event_risk, load_event_risk
from .fundamentals import attach_fundamental_factors, load_fundamentals
from .performance import records_from_columns
from .risk_blacklist import attach_risk_blacklist, load_risk_blacklist
from .scoring import build_market_regime, candidate_filter_report, prepare_candidates


class CandidatePipeline:
    """Builds recommendation candidate frames and side inputs from live quotes."""

    def __init__(self, provider, caches) -> None:
        self.provider = provider
        self.caches = caches

    def current_quotes(self) -> pd.DataFrame:
        quotes = self.caches.quotes_cache.get()
        if quotes is None:
            quotes = self.provider.get_realtime_quotes()
            self.caches.quotes_cache.set(quotes)
        return quotes

    def current_quotes_or_empty(self) -> Tuple[pd.DataFrame, str]:
        quotes = self.caches.quotes_cache.get()
        if quotes is not None:
            return quotes, ""
        try:
            quotes = self.provider.get_realtime_quotes()
            self.caches.quotes_cache.set(quotes)
            return quotes, ""
        except Exception as exc:
            return pd.DataFrame(), str(exc)

    def attach_static_risk_layers(self, candidates: pd.DataFrame) -> pd.DataFrame:
        payload = load_event_risk(self.provider)
        candidates = attach_event_risk(candidates, payload)
        candidates = attach_risk_blacklist(candidates, load_risk_blacklist())
        codes = candidates["code"].tolist() if candidates is not None and "code" in candidates.columns else []
        return attach_fundamental_factors(candidates, load_fundamentals(self.provider, codes=codes))

    def candidates_with_regime(
        self,
        quotes: pd.DataFrame,
        attach_codes=None,
    ) -> Tuple[pd.DataFrame, Dict[str, object]]:
        candidates = self.attach_static_risk_layers(prepare_candidates(quotes))
        if attach_codes:
            candidates = attach_alphalite_factors_for_codes(self.provider, candidates, attach_codes)
        else:
            candidates = attach_alphalite_factors(self.provider, self.caches.factors_cache, candidates)
        market_regime = build_market_regime(candidates, breadth_source=quotes)
        return candidates, market_regime

    def hot_ranks(self) -> Dict[str, int]:
        hot_ranks = self.caches.hot_cache.get()
        if hot_ranks is not None:
            return hot_ranks
        if config.ENABLE_HOT_RANKS:
            try:
                hot_ranks = self.provider.get_hot_ranks()
            except Exception:
                hot_ranks = {}
        else:
            hot_ranks = {}
        self.caches.hot_cache.set(hot_ranks)
        return hot_ranks

    def industry_strength(self) -> Dict[str, float]:
        industry_strength = self.caches.industry_cache.get()
        if industry_strength is not None:
            return industry_strength
        if config.ENABLE_INDUSTRY_STRENGTH:
            try:
                industry_strength = self.provider.get_industry_strength()
            except Exception:
                industry_strength = {}
        else:
            industry_strength = {}
        self.caches.industry_cache.set(industry_strength)
        return industry_strength

    def recommendation_input_context(self) -> Dict[str, object]:
        quotes = self.current_quotes()
        hard_filter_report = candidate_filter_report(quotes)
        candidates, market_regime = self.candidates_with_regime(quotes)
        sentiment_lookup = sentiment_for_candidates(
            self.provider,
            self.caches.sentiment_cache,
            records_from_columns(candidates, ["code", "name"], limit=80, sort_by="pct_chg", ascending=False),
        )
        return {
            "quotes": quotes,
            "hard_filter_report": hard_filter_report,
            "candidates": candidates,
            "market_regime": market_regime,
            "hot_ranks": self.hot_ranks(),
            "industry_strength": self.industry_strength(),
            "sentiment_lookup": sentiment_lookup,
        }

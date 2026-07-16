from __future__ import annotations

import time
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
from .scoring_core.candidate_filters import candidate_filter_report, prepare_candidates
from .scoring_core.market_regime import build_market_regime
from .services.factor_sentiment_refresh import FactorSentimentRefreshService
from .services.recommendation_quotes import RecommendationQuoteRefreshService


class CandidatePipeline:
    """Builds recommendation candidate frames and side inputs from live quotes."""

    def __init__(
        self,
        provider,
        caches,
        *,
        quote_refresh_service: RecommendationQuoteRefreshService | None = None,
        factor_sentiment_refresh: FactorSentimentRefreshService | None = None,
    ) -> None:
        self.provider = provider
        self.caches = caches
        self.quote_refresh_service = quote_refresh_service or self._build_compat_quote_refresh_service(provider, caches)
        self.factor_sentiment_refresh = factor_sentiment_refresh

    @staticmethod
    def _build_compat_quote_refresh_service(provider, caches) -> RecommendationQuoteRefreshService | None:
        fetch_quotes = getattr(provider, "get_recommendation_quotes", None)
        quotes_cache = getattr(caches, "quotes_cache", None)
        display_cache = getattr(caches, "recommendation_quotes_cache", None)
        recommendation_cache = getattr(caches, "recommendation_cache", None)
        horizon_cache = getattr(caches, "horizon_cache", None)
        capabilities = (
            fetch_quotes,
            getattr(quotes_cache, "get", None),
            getattr(display_cache, "set", None),
            getattr(recommendation_cache, "clear", None),
            getattr(horizon_cache, "clear", None),
        )
        if not all(callable(capability) for capability in capabilities):
            return None
        return RecommendationQuoteRefreshService(
            fetch_quotes=fetch_quotes,
            load_full_quotes=quotes_cache.get,
            cache_display_quotes=display_cache.set,
            clear_recommendation_cache=recommendation_cache.clear,
            clear_horizon_cache=horizon_cache.clear,
        )

    def current_quotes(self) -> pd.DataFrame:
        quotes = self.caches.quotes_cache.get()
        if quotes is None:
            quotes = self.provider.get_realtime_quotes()
            self.caches.quotes_cache.set(quotes)
        return self._overlay_candidate_quotes(quotes)

    def current_quotes_or_empty(self) -> Tuple[pd.DataFrame, str]:
        quotes = self.caches.quotes_cache.get()
        if quotes is not None:
            return self._overlay_candidate_quotes(quotes), ""
        try:
            quotes = self.provider.get_realtime_quotes()
            self.caches.quotes_cache.set(quotes)
            return self._overlay_candidate_quotes(quotes), ""
        except Exception as exc:
            return pd.DataFrame(), str(exc)

    def refresh_quotes(self) -> Tuple[pd.DataFrame, str]:
        refresh = getattr(self.provider, "refresh_realtime_quotes_async", None)
        status = getattr(self.provider, "quote_refresh_status", None)
        if callable(refresh):
            refresh(force=True)
        if callable(status):
            deadline = time.monotonic() + max(
                0.5,
                float(getattr(config, "QUOTE_REFRESH_WAIT_SECONDS", 12)),
            )
            while time.monotonic() < deadline and bool((status() or {}).get("running")):
                time.sleep(0.1)
        self.caches.quotes_cache.clear()
        return self.current_quotes_or_empty()

    def recommendation_quotes(self, codes) -> Tuple[pd.DataFrame, str]:
        if self.quote_refresh_service is None:
            return pd.DataFrame(), "推荐池行情刷新服务不可用"
        return self.quote_refresh_service.recommendation_quotes(codes)

    def refresh_recommendation_quote_groups(self, profile) -> None:
        if self.quote_refresh_service is not None:
            self.quote_refresh_service.refresh_groups(profile)

    def recommendation_quote_status(self):
        if self.quote_refresh_service is None:
            return {}
        return self.quote_refresh_service.status()

    def _overlay_candidate_quotes(self, quotes):
        if self.quote_refresh_service is None:
            return quotes
        return self.quote_refresh_service.overlay_candidate_quotes(quotes)

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
            candidates = attach_alphalite_factors(
                self.provider,
                self.caches.factors_cache,
                candidates,
                refresh_service=self.factor_sentiment_refresh,
            )
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
            refresh_service=self.factor_sentiment_refresh,
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

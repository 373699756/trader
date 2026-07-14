from __future__ import annotations

import threading
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


class CandidatePipeline:
    """Builds recommendation candidate frames and side inputs from live quotes."""

    def __init__(self, provider, caches) -> None:
        self.provider = provider
        self.caches = caches
        self._recommendation_quotes_lock = threading.Lock()
        self._recommendation_quotes_network_lock = threading.Lock()
        self._recommendation_watched_codes = set()
        self._recommendation_group_state = {
            "display": {"running": False, "last_started": 0.0, "last_success": 0.0, "error": "", "snapshot": pd.DataFrame()},
            "candidate": {"running": False, "last_started": 0.0, "last_success": 0.0, "error": "", "snapshot": pd.DataFrame()},
        }

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
        normalized_codes = sorted(
            {
                str(code or "").strip()[-6:]
                for code in (codes or [])
                if len(str(code or "").strip()) >= 6
            }
        )
        if not normalized_codes:
            return pd.DataFrame(), "推荐池没有股票代码"
        with self._recommendation_quotes_lock:
            self._recommendation_watched_codes.update(normalized_codes)
            state = self._recommendation_group_state["display"]
            snapshot = state["snapshot"]
            status = str(state["error"] or "")
            if state["running"] and not status:
                status = "推荐池行情后台刷新中"
        self._start_recommendation_group_refresh("display", normalized_codes, 0)
        return (snapshot if snapshot is not None else pd.DataFrame()), status

    def refresh_recommendation_quote_groups(self, profile) -> None:
        recommendation_seconds = profile.get("recommendation_seconds")
        candidate_seconds = profile.get("candidate_seconds")
        with self._recommendation_quotes_lock:
            display_codes = sorted(self._recommendation_watched_codes)
        if recommendation_seconds is not None and display_codes:
            self._start_recommendation_group_refresh("display", display_codes, float(recommendation_seconds))
        if candidate_seconds is not None:
            candidate_codes = self._select_candidate_codes()
            if candidate_codes:
                self._start_recommendation_group_refresh("candidate", candidate_codes, float(candidate_seconds))

    def recommendation_quote_status(self):
        with self._recommendation_quotes_lock:
            return {
                key: {
                    "running": bool(value["running"]),
                    "last_success_monotonic": float(value["last_success"]),
                    "error": str(value["error"] or ""),
                    "row_count": int(len(value["snapshot"])) if value["snapshot"] is not None else 0,
                }
                for key, value in self._recommendation_group_state.items()
            }

    def _start_recommendation_group_refresh(self, group, normalized_codes, interval_seconds) -> bool:
        now = time.monotonic()
        with self._recommendation_quotes_lock:
            state = self._recommendation_group_state[group]
            if state["running"] or (state["last_started"] and now - state["last_started"] < interval_seconds):
                return False
            state["running"] = True
            state["last_started"] = now
        worker = threading.Thread(
            target=self._refresh_recommendation_group_worker,
            args=(group, list(normalized_codes)),
            name=f"recommendation-{group}-quotes-refresh",
            daemon=True,
        )
        try:
            worker.start()
        except Exception as exc:
            with self._recommendation_quotes_lock:
                state = self._recommendation_group_state[group]
                state["running"] = False
                state["error"] = str(exc)
            return False
        return True

    def _refresh_recommendation_group_worker(self, group, normalized_codes) -> None:
        try:
            with self._recommendation_quotes_network_lock:
                quotes = self.provider.get_recommendation_quotes(normalized_codes)
        except Exception as exc:
            with self._recommendation_quotes_lock:
                state = self._recommendation_group_state[group]
                state["error"] = str(exc)
                state["running"] = False
            return
        with self._recommendation_quotes_lock:
            state = self._recommendation_group_state[group]
            state["snapshot"] = quotes
            state["error"] = ""
            state["last_success"] = time.monotonic()
            state["running"] = False
        if group == "display":
            self.caches.recommendation_quotes_cache.set(quotes)
        else:
            self.caches.recommendation_cache.clear()
            self.caches.horizon_cache.clear()

    def _select_candidate_codes(self):
        quotes = self.caches.quotes_cache.get()
        if quotes is None or quotes.empty or "code" not in quotes.columns:
            return []
        frame = quotes.copy()
        score = pd.Series(0.0, index=frame.index)
        for column, weight in (("turnover", 0.45), ("pct_chg", 0.3), ("volume_ratio", 0.25)):
            if column in frame.columns:
                values = pd.to_numeric(frame[column], errors="coerce").fillna(0.0)
                score += values.rank(pct=True) * weight
        frame["_candidate_refresh_score"] = score
        if "price" in frame.columns:
            frame = frame[pd.to_numeric(frame["price"], errors="coerce").fillna(0.0) > 0]
        size = max(1, int(getattr(config, "RECOMMENDATION_CANDIDATE_POOL_SIZE", 150)))
        return frame.nlargest(size, "_candidate_refresh_score")["code"].astype(str).tolist()

    def _overlay_candidate_quotes(self, quotes):
        if quotes is None or quotes.empty or "code" not in quotes.columns:
            return quotes
        with self._recommendation_quotes_lock:
            candidate = self._recommendation_group_state["candidate"]["snapshot"]
        if candidate is None or candidate.empty or "code" not in candidate.columns:
            return quotes
        result = quotes.copy()
        update_fields = (
            "price", "pct_chg", "volume_ratio", "turnover_rate", "turnover", "volume",
            "amplitude", "high", "low", "open", "quote_timestamp", "quote_source",
        )
        candidate_index = candidate.drop_duplicates("code", keep="last").set_index("code")
        codes = result["code"].astype(str)
        for field in update_fields:
            if field not in candidate_index.columns:
                continue
            updates = codes.map(candidate_index[field])
            if field in result.columns:
                result[field] = updates.where(updates.notna(), result[field])
            else:
                result[field] = updates
        result.attrs.update(getattr(quotes, "attrs", {}) or {})
        result.attrs["candidate_quote_timestamp"] = str((candidate.attrs or {}).get("quote_timestamp") or "")
        return result

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

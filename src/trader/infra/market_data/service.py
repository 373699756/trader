"""Market-data port coordinator composed from typed, state-owning components."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from trader.application.ports.market import MarketSnapshotMetadata
from trader.application.ports.types import JsonObject
from trader.domain.market.models import (
    FeatureSnapshot,
    LiveQuote,
)
from trader.domain.outcome.models import OutcomeBar
from trader.infra.market_data.service_candidates import QuoteStore
from trader.infra.market_data.service_execution import MarketTaskRunner
from trader.infra.market_data.service_health import MarketDataHealth
from trader.infra.market_data.service_history import HistoryStore
from trader.infra.market_data.service_history_warmup import HistoryWarmup
from trader.infra.market_data.service_intraday import IntradayLoader
from trader.infra.market_data.service_research import ResearchLoader
from trader.infra.market_data.service_support import _history_preload_codes, _normalize_codes
from trader.infra.market_data.service_tushare import ReferenceLoader


@dataclass(frozen=True)
class MarketFeatureDependencies:
    quotes: QuoteStore
    history: HistoryStore
    warmup: HistoryWarmup
    research: ResearchLoader
    intraday: IntradayLoader
    references: ReferenceLoader
    runner: MarketTaskRunner
    health: MarketDataHealth


class MarketFeatureService:
    def __init__(
        self,
        dependencies: MarketFeatureDependencies,
        *,
        history_preload_limit: int,
    ) -> None:
        self.quotes = dependencies.quotes
        self.history = dependencies.history
        self.warmup = dependencies.warmup
        self.research = dependencies.research
        self.intraday = dependencies.intraday
        self.references = dependencies.references
        self.runner = dependencies.runner
        self.health_reporter = dependencies.health
        self.history_preload_limit = max(1, history_preload_limit)

    def fetch_market_features(
        self,
        observed_at: datetime,
        *,
        force: bool = False,
        deadline: datetime | None = None,
    ) -> Sequence[FeatureSnapshot]:
        cached = self.quotes.cached_market_features(force=force)
        if cached is not None:
            return cached
        quotes = tuple(
            self.runner.run_data_task_until(
                deadline,
                False,
                self.quotes.gateway.fetch_market,
                observed_at=observed_at,
                force=force,
                deadline=deadline,
            )
        )
        history_codes = _history_preload_codes(quotes, self.history_preload_limit)
        if self.runner.source_lanes is not None:
            self.warmup.schedule_history_warmup(history_codes, observed_at)
        action_restrictions: dict[str, set[str]] = {}
        histories = (
            self.history.load(
                history_codes,
                deadline=deadline,
                action_restrictions=action_restrictions,
            )
            if self.runner.source_lanes is None
            else self.history.cached(
                history_codes,
                fresh_only=True,
                action_restrictions=action_restrictions,
            )
        )
        self.runner.ensure_before_deadline(deadline)
        features = self.quotes.build_market_features(
            quotes,
            histories,
            observed_at,
            action_restrictions=action_restrictions,
        )
        self.runner.ensure_before_deadline(deadline)
        published = self.quotes.publish_market_features(features)
        self.history.update_coverage(history_codes, tuple(quote.data_version for quote in quotes))
        return published

    def fetch_candidate_features(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        include_intraday_tail: bool = False,
        include_structured_research: bool = False,
    ) -> Sequence[FeatureSnapshot]:
        normalized = _normalize_codes(codes)
        if not normalized:
            return ()
        self.refresh_candidate_quotes(normalized, observed_at)
        quotes = self.quotes.candidate_snapshot(normalized)
        if {quote.code for quote in quotes} != set(normalized):
            self.fetch_market_features(observed_at)
            quotes = self.quotes.candidate_snapshot(normalized)
        action_restrictions: dict[str, set[str]] = {}
        histories = self.history.load(normalized, action_restrictions=action_restrictions)
        research = self.research.load(
            normalized,
            observed_at,
            include_structured=include_structured_research,
            action_restrictions=action_restrictions,
        )
        intraday = (
            self.intraday.load(normalized, observed_at, action_restrictions=action_restrictions)
            if include_intraday_tail
            else None
        )
        features = self.quotes.build_candidate_features(
            quotes,
            histories,
            observed_at,
            research_observations=research,
            intraday_minutes=intraday,
            action_restrictions=action_restrictions,
        )
        if include_intraday_tail:
            self.intraday.record_feature_coverage(normalized, features)
        return features

    def refresh_candidate_quotes(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        force: bool = False,
        deadline: datetime | None = None,
    ) -> Sequence[FeatureSnapshot]:
        normalized = _normalize_codes(codes)
        if not normalized:
            return ()
        fetched = tuple(
            self.runner.run_data_task_until(
                deadline,
                True,
                self.quotes.gateway.fetch_candidates,
                normalized,
                observed_at=observed_at,
                force=force,
                deadline=deadline,
            )
        )
        self.quotes.update_candidate_quotes(fetched)
        resolved = self.quotes.candidate_snapshot(normalized)
        action_restrictions: dict[str, set[str]] = {}
        return self.quotes.build_candidate_features(
            resolved,
            self.history.cached(normalized, action_restrictions=action_restrictions),
            observed_at,
            research_observations=self.research.cached(
                normalized,
                include_structured=False,
                action_restrictions=action_restrictions,
            ),
            intraday_minutes=None,
            action_restrictions=action_restrictions,
        )

    def refresh_industry_heat(self, observed_at: datetime) -> Sequence[FeatureSnapshot]:
        quotes = self.quotes.market_quotes()
        if not quotes:
            return ()
        action_restrictions: dict[str, set[str]] = {}
        histories = self.history.cached(
            tuple(quote.code for quote in quotes),
            action_restrictions=action_restrictions,
        )
        features = self.quotes.build_market_features(
            quotes,
            histories,
            observed_at,
            action_restrictions=action_restrictions,
        )
        return self.quotes.publish_market_features(features)

    def refresh_market_news(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        deadline: datetime | None = None,
    ) -> None:
        self.research.load(
            _normalize_codes(codes),
            observed_at,
            include_structured=False,
            force=True,
            deadline=deadline,
        )

    def refresh_stock_risk(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        deadline: datetime | None = None,
    ) -> None:
        self.research.load(
            _normalize_codes(codes),
            observed_at,
            include_structured=True,
            deadline=deadline,
        )

    def refresh_reference_data(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        force: bool = False,
    ) -> None:
        self.references.refresh_reference_data(codes, observed_at, force=force)

    def schedule_reference_data(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        force: bool = False,
    ) -> None:
        self.references.schedule_reference_data(codes, observed_at, force=force)

    def refresh_intraday_tail(self, codes: Sequence[str], observed_at: datetime) -> None:
        self.intraday.load(_normalize_codes(codes), observed_at)

    def read_candidate_features(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        include_intraday_tail: bool = False,
        include_structured_research: bool = False,
    ) -> Sequence[FeatureSnapshot]:
        normalized = _normalize_codes(codes)
        if not normalized:
            return ()
        action_restrictions: dict[str, set[str]] = {}
        histories = self.history.cached(normalized, action_restrictions=action_restrictions)
        research = self.research.cached(
            normalized,
            include_structured=include_structured_research,
            action_restrictions=action_restrictions,
        )
        intraday = (
            self.intraday.cached(normalized, action_restrictions=action_restrictions) if include_intraday_tail else None
        )
        features = self.quotes.build_candidate_features(
            self.quotes.candidate_snapshot(normalized),
            histories,
            observed_at,
            research_observations=research,
            intraday_minutes=intraday,
            action_restrictions=action_restrictions,
        )
        if include_intraday_tail:
            self.intraday.record_feature_coverage(normalized, features)
        return features

    def current_quotes(self, codes: Sequence[str]) -> Mapping[str, LiveQuote]:
        normalized = _normalize_codes(codes)
        return self.quotes.current_quotes(normalized)

    def read_outcome_bars(
        self,
        codes: Sequence[str],
        observed_at: datetime,
    ) -> Mapping[str, tuple[OutcomeBar, ...]]:
        return self.history.read_outcome_bars(_normalize_codes(codes), observed_at)

    def health(self) -> JsonObject:
        return self.health_reporter.health()

    def snapshot_metadata(self, codes: Sequence[str] | None = None) -> MarketSnapshotMetadata:
        return self.health_reporter.snapshot_metadata(codes)


__all__ = ["MarketFeatureService"]

"""Market-data port coordinator composed from typed, state-owning components."""

from __future__ import annotations

import hashlib
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from trader.application.ports.market import MarketSnapshotMetadata, ResearchRefreshResult
from trader.application.ports.types import JsonObject
from trader.domain.market.models import (
    Board,
    FeatureSnapshot,
    LiveQuote,
    MarketQuote,
)
from trader.domain.outcome.models import OutcomeBar
from trader.infra.market_data.market_cache_identity import (
    _history_preload_codes,
    _normalize_codes,
    _research_data_version,
)
from trader.infra.market_data.service_candidates import QuoteCache
from trader.infra.market_data.service_execution import MarketTaskRunner
from trader.infra.market_data.service_health import MarketDataHealth
from trader.infra.market_data.service_history import HistoryCache
from trader.infra.market_data.service_history_warmup import HistoryWarmup
from trader.infra.market_data.service_intraday import IntradayLoader
from trader.infra.market_data.service_research import ResearchLoader
from trader.infra.market_data.service_research_models import research_component_coverage
from trader.infra.market_data.service_tushare import ReferenceLoader


@dataclass(frozen=True)
class MarketFeatureDependencies:
    quotes: QuoteCache
    history: HistoryCache
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
            if self.runner.source_lanes is not None:
                history_codes = _history_preload_codes(
                    tuple(feature.quote for feature in cached),
                    self.history_preload_limit,
                )
                self.warmup.schedule_history_warmup(history_codes, observed_at)
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
                fresh_only=False,
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
            self.intraday.load(
                _board_fair_codes(normalized, quotes),
                observed_at,
                action_restrictions=action_restrictions,
            )
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

    def refresh_topk_quotes(
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
                self.quotes.gateway.fetch_topk_quotes,
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

    def refresh_long_quotes(
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
        quotes = tuple(
            self.quotes.gateway.fetch_long_quotes(
                normalized,
                observed_at=observed_at,
                force=force,
                deadline=deadline,
            )
        )
        return tuple(
            FeatureSnapshot(
                quote=quote,
                values={},
                observed_at=observed_at,
                missing_fields=tuple(
                    field
                    for field in ("price", "pct_change", "amount", "turnover_rate", "market_cap")
                    if getattr(quote, field) is None
                ),
            )
            for quote in quotes
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
    ) -> ResearchRefreshResult:
        requested = _normalize_codes(codes)
        started_at = self.runner.wall_clock()
        report = self.research.load_report(
            requested,
            observed_at,
            include_structured=True,
            deadline=deadline,
        )
        completed_at = self.runner.wall_clock()
        observations = report.observations
        deferred = set(report.deferred_codes)
        failed = tuple(
            code
            for code in requested
            if code not in deferred
            and (code not in observations or not any(research_component_coverage(observations[code])))
        )
        failed_set = set(failed)
        completed = tuple(
            code for code in requested if code in observations and code not in deferred and code not in failed_set
        )
        completed_set = set(completed)
        covered = tuple(code for code in completed if all(research_component_coverage(observations[code])))
        partial = tuple(code for code in completed if code not in covered and code not in failed)
        version_material = "|".join(
            f"{code}:{_research_data_version(observations[code])}" for code in sorted(observations)
        )
        data_version = (
            f"research-batch:{hashlib.sha256(version_material.encode('utf-8')).hexdigest()[:20]}"
            if version_material
            else "research-batch:empty"
        )
        return ResearchRefreshResult(
            requested_codes=requested,
            completed_codes=completed,
            changed_codes=tuple(code for code in report.changed_codes if code in completed_set),
            partial_codes=partial,
            failed_codes=failed,
            deferred_codes=report.deferred_codes,
            covered_codes=covered,
            data_version=data_version,
            started_at=started_at,
            completed_at=completed_at,
            deadline_reached=report.deadline_reached,
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
        security_master_codes: Sequence[str] | None = None,
    ) -> None:
        self.references.schedule_reference_data(
            codes,
            observed_at,
            force=force,
            security_master_codes=security_master_codes,
        )
        self.warmup.schedule_history_warmup(codes, observed_at)

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

    def cached_quotes(self, codes: Sequence[str]) -> Mapping[str, MarketQuote]:
        """Return the newest cached full quote without performing external I/O."""
        normalized = _normalize_codes(codes)
        market = {quote.code: quote for quote in self.quotes.market_quotes()}
        candidates = self.quotes.candidate_entries()
        result: dict[str, MarketQuote] = {}
        for code in normalized:
            available = tuple(quote for quote in (market.get(code), candidates.get(code)) if quote is not None)
            if available:
                result[code] = max(
                    available,
                    key=lambda quote: (quote.source_time, quote.received_time, quote.data_version),
                )
        return result

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


def _board_fair_codes(codes: Sequence[str], quotes: Sequence[MarketQuote]) -> tuple[str, ...]:
    by_code = {quote.code: quote for quote in quotes}
    board_order = (Board.MAIN, Board.CHINEXT, Board.STAR)
    queues = {
        board: deque(code for code in codes if by_code.get(code) is not None and by_code[code].board is board)
        for board in board_order
    }
    trailing = [code for code in codes if code not in by_code or by_code[code].board is Board.UNSUPPORTED]
    ordered: list[str] = []
    while any(queues.values()):
        for board in board_order:
            if queues[board]:
                ordered.append(queues[board].popleft())
    return tuple((*ordered, *trailing))


__all__ = ["MarketFeatureService"]

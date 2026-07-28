"""Read-only tomorrow v2 feature assembly and deterministic local selection."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime

from trader.application.policy import RecommendationPolicy
from trader.application.ports.market import MarketDataPlaneSnapshot, RealtimeDataPlaneReaderPort
from trader.domain.market.models import Board, FeatureSnapshot, LiveQuote, MarketQuote
from trader.domain.recommendation.models import Strategy
from trader.domain.recommendation.tomorrow_selection import (
    BoardCrossSectionFallback,
    TomorrowSelectionPolicy,
    TomorrowSelectionRequest,
    TomorrowSelectionResult,
    select_tomorrow,
)

_SUPPORTED_BOARDS = (Board.MAIN, Board.CHINEXT, Board.STAR)


class TomorrowSelectionNotReadyError(RuntimeError):
    """A coherent current market epoch is not available for local selection."""


class TomorrowSelectionUseCase:
    def __init__(self, reader: RealtimeDataPlaneReaderPort, policy: RecommendationPolicy) -> None:
        self._reader = reader
        self._policy = policy

    def execute(
        self,
        *,
        evaluated_at: datetime,
        max_age_seconds: float,
        phase: str = "tomorrow",
        fallbacks: Mapping[Board, BoardCrossSectionFallback] | None = None,
    ) -> TomorrowSelectionResult:
        snapshot = self._reader.snapshot()
        if snapshot.daily_features is None or snapshot.market is None:
            raise TomorrowSelectionNotReadyError("coherent_market_epoch_unavailable")
        if evaluated_at.date() != snapshot.market.trade_date:
            raise TomorrowSelectionNotReadyError("market_epoch_trade_date_mismatch")
        epoch_times = [snapshot.daily_features.observed_at, snapshot.market.observed_at]
        if snapshot.candidate_quotes is not None:
            epoch_times.append(snapshot.candidate_quotes.observed_at)
        if max(epoch_times) > evaluated_at:
            raise TomorrowSelectionNotReadyError("market_epoch_from_future")
        board_policies = {
            board: board_policy
            for board in _SUPPORTED_BOARDS
            if (board_policy := self._policy.board_policy(Strategy.TOMORROW, board)) is not None
        }
        threshold = self._policy.selection.thresholds.get("tomorrow", 0.0)
        selection_policy = TomorrowSelectionPolicy(
            board_policies=board_policies,
            risk_rules=self._policy.risk_rules,
            max_age_seconds=max_age_seconds,
            local_risk_cap=self._policy.fusion.local_risk_cap,
            candidate_limit_per_board=120,
            top_k=min(self._policy.selection.default_top_k, 10),
            maximum_per_industry=self._policy.selection.maximum_per_industry,
            minimum_local_score=max(0.0, threshold - self._policy.selection.observation_margin),
            hard_filter=self._policy.hard_filter,
        )
        features = assemble_tomorrow_features(snapshot)
        merge_epochs = {feature.merge_epoch for feature in features}
        if len(merge_epochs) != 1:
            raise TomorrowSelectionNotReadyError("feature_merge_epoch_mismatch")
        return select_tomorrow(
            TomorrowSelectionRequest(
                features=features,
                evaluated_at=evaluated_at,
                trade_date=snapshot.market.trade_date.isoformat(),
                phase=phase,
                data_version=snapshot.market.content_hash,
                merge_epoch=next(iter(merge_epochs)),
                policy=selection_policy,
                fallbacks=fallbacks or {},
            )
        )


def assemble_tomorrow_features(snapshot: MarketDataPlaneSnapshot) -> tuple[FeatureSnapshot, ...]:
    daily = snapshot.daily_features
    market = snapshot.market
    if daily is None or market is None:
        raise TomorrowSelectionNotReadyError("coherent_market_epoch_unavailable")
    rows = {row.code: row for row in daily.rows}
    quotes = {quote.code: quote for quote in market.quotes}
    candidate_quotes = (
        {quote.code: quote for quote in snapshot.candidate_quotes.quotes}
        if snapshot.candidate_quotes is not None
        else {}
    )
    candidate_features = (
        {row.code: row for row in snapshot.candidate_quotes.feature_rows}
        if snapshot.candidate_quotes is not None
        else {}
    )
    unknown_candidates = sorted(set(candidate_quotes).difference(quotes))
    if unknown_candidates:
        raise TomorrowSelectionNotReadyError("candidate_quote_not_in_market_epoch")
    candidate_epoch_is_effective = any(
        quote.source_time >= quotes[code].source_time for code, quote in candidate_quotes.items()
    )
    merge_epoch = market.version
    if candidate_epoch_is_effective and snapshot.candidate_quotes is not None:
        merge_epoch = f"{merge_epoch}|{snapshot.candidate_quotes.version}"

    result: list[FeatureSnapshot] = []
    for code in sorted(quotes):
        market_quote = quotes[code]
        candidate_quote = candidate_quotes.get(code)
        candidate_is_current = candidate_quote is not None and candidate_quote.source_time >= market_quote.source_time
        quote = _apply_candidate_quote(market_quote, candidate_quote)
        row = rows.get(code)
        candidate_row = candidate_features.get(code) if candidate_is_current else None
        values = dict(row.values) if row is not None else {}
        missing_fields = set(row.missing_fields) if row is not None else {"daily_features"}
        missing_reasons = (
            dict(row.missing_reasons) if row is not None else {"daily_features": "daily feature row is unavailable"}
        )
        if candidate_row is not None:
            values.update(candidate_row.values)
            for name, value in candidate_row.values.items():
                if value is None:
                    missing_fields.add(name)
                    missing_reasons.setdefault(name, "candidate realtime feature is unavailable")
                else:
                    missing_fields.discard(name)
                    missing_reasons.pop(name, None)
            missing_fields.update(candidate_row.missing_fields)
            missing_reasons.update(candidate_row.missing_reasons)
        result.append(
            FeatureSnapshot(
                quote=quote,
                values=values,
                observed_at=(
                    max(market.observed_at, snapshot.candidate_quotes.observed_at)
                    if candidate_is_current and snapshot.candidate_quotes is not None
                    else market.observed_at
                ),
                history_days=row.history_sessions if row is not None else 0,
                market_regime=market.market_regime,
                missing_fields=tuple(sorted(missing_fields)),
                missing_reasons=missing_reasons,
                merge_epoch=merge_epoch,
            )
        )
    return tuple(result)


def _apply_candidate_quote(market: MarketQuote, candidate: LiveQuote | None) -> MarketQuote:
    if candidate is None or candidate.source_time < market.source_time:
        return market
    high = max(value for value in (market.high, candidate.price) if value is not None)
    low = min(value for value in (market.low, candidate.price) if value is not None)
    return replace(
        market,
        price=candidate.price,
        high=high,
        low=low,
        pct_change=candidate.pct_change,
        source=candidate.source,
        source_time=candidate.source_time,
        received_time=candidate.received_time,
        data_version=candidate.data_version,
        cross_source_deviation_pct=candidate.cross_source_deviation_pct,
        cross_source_verified=candidate.cross_source_verified,
    )


__all__ = [
    "TomorrowSelectionNotReadyError",
    "TomorrowSelectionUseCase",
    "assemble_tomorrow_features",
]

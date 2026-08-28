"""Bind Score-R2 evidence to pure Tomorrow point-in-time feature engineering."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from trader.application.research.models import HistoricalDaySummary, HistoricalFullFieldBundle
from trader.application.research.tomorrow_feature_models import (
    TomorrowFeatureContext,
    TomorrowFeatureContextBatch,
    TomorrowPointInTimeFeatureBatch,
)
from trader.domain.research.tomorrow_features import (
    DailyFeaturePoint,
    IntradayFeaturePoint,
    TomorrowFeatureStockInput,
    build_tomorrow_stock_features,
)


class ScoreTomorrowPointInTimeFeatures:
    """Build an immutable research-only feature batch from one R2 day."""

    def build(
        self,
        summary: HistoricalDaySummary,
        bundle: HistoricalFullFieldBundle,
        contexts: TomorrowFeatureContextBatch,
    ) -> TomorrowPointInTimeFeatureBatch:
        _validate_parent_identity(summary, bundle, contexts)
        summary_by_code = {item.code: item for item in summary.candidates}
        full_by_code = {item.code: item for item in bundle.candidates}
        context_by_code = {item.code: item for item in contexts.contexts}
        daily_by_code = _daily_points(bundle)
        minute_by_code = _minute_points(bundle)
        stocks: list[TomorrowFeatureStockInput] = []
        for code in bundle.requested_codes:
            context = context_by_code[code]
            candidate = summary_by_code[code]
            full = full_by_code[code]
            _validate_stock_identity(candidate.industry, candidate.board, full.board, full.feature_as_of, context)
            stocks.append(
                TomorrowFeatureStockInput(
                    code=code,
                    board=context.board,
                    industry=context.industry,
                    industry_effective_at=context.industry_effective_at,
                    industry_received_at=context.industry_received_at,
                    as_of=context.observed_at,
                    daily_points=daily_by_code[code],
                    intraday_points=minute_by_code[code],
                    current_open=context.current_open,
                    current_high=context.current_high,
                    current_low=context.current_low,
                    current_last=context.current_last,
                    market_cap=context.market_cap,
                    liquidity=context.liquidity,
                    published_facts=context.published_facts,
                )
            )
        return TomorrowPointInTimeFeatureBatch(
            trade_date=summary.trade_date,
            observed_at=summary.observed_at,
            input_hash=summary.input_hash,
            context_hash=contexts.content_hash,
            rows=build_tomorrow_stock_features(tuple(stocks)),
        )


def _validate_parent_identity(
    summary: HistoricalDaySummary,
    bundle: HistoricalFullFieldBundle,
    contexts: TomorrowFeatureContextBatch,
) -> None:
    if summary.trade_date != bundle.trade_date or summary.trade_date != contexts.trade_date:
        raise ValueError("Tomorrow feature trade date does not match R2 evidence")
    if summary.input_hash != bundle.input_hash or summary.input_hash != contexts.input_hash:
        raise ValueError("Tomorrow feature input hash does not match R2 evidence")
    requested = set(bundle.requested_codes)
    if {item.code for item in contexts.contexts} != requested:
        raise ValueError("Tomorrow feature contexts must exactly cover R2 requested codes")
    if not requested.issubset({item.code for item in summary.candidates}):
        raise ValueError("Tomorrow feature codes must belong to the R2 day summary")


def _validate_stock_identity(
    industry: str,
    summary_board: str,
    full_board: str,
    feature_as_of: datetime,
    context: TomorrowFeatureContext,
) -> None:
    if context.board != summary_board or context.board != full_board:
        raise ValueError("Tomorrow feature board does not match R2 evidence")
    if context.industry != industry:
        raise ValueError("Tomorrow feature industry does not match R2 evidence")
    if context.observed_at != feature_as_of:
        raise ValueError("Tomorrow feature cutoff does not match R2 full fields")


def _daily_points(bundle: HistoricalFullFieldBundle) -> dict[str, tuple[DailyFeaturePoint, ...]]:
    grouped: defaultdict[str, list[DailyFeaturePoint]] = defaultdict(list)
    for item in bundle.daily_bars:
        grouped[item.code].append(DailyFeaturePoint(item.session_date, item.close, item.amount))
    return {code: tuple(sorted(values, key=lambda item: item.session_date)) for code, values in grouped.items()}


def _minute_points(bundle: HistoricalFullFieldBundle) -> dict[str, tuple[IntradayFeaturePoint, ...]]:
    grouped: defaultdict[str, list[IntradayFeaturePoint]] = defaultdict(list)
    for item in bundle.minute_bars:
        grouped[item.code].append(IntradayFeaturePoint(item.minute, item.close, item.amount))
    return {code: tuple(sorted(values, key=lambda item: item.observed_at)) for code, values in grouped.items()}


__all__ = ["ScoreTomorrowPointInTimeFeatures"]

from __future__ import annotations
from dataclasses import replace
from datetime import date, timedelta
from tests.helpers.training.research.application.historical_ports import TRADE_DATE, _bundle, _summary
from trader.recommendation.domain.market.data_plane import MarketDataPlaneSnapshot
from trader.training.application.historical_extraction_models import (
    HistoricalDaySummary,
    HistoricalEvaluatedCandidate,
    HistoricalFullFieldBundle,
)
from trader.training.domain.evaluation.historical import ResearchDataLineage


class _Port:
    def __init__(self, summary: HistoricalDaySummary | None = None) -> None:
        self._summary = summary or _summary()

    def snapshot(self) -> MarketDataPlaneSnapshot:
        return MarketDataPlaneSnapshot(None, None, None, None)

    def is_trading_day(self, trade_date: date) -> bool:
        return trade_date == TRADE_DATE

    def read_day_summary(self, trade_date: date) -> HistoricalDaySummary:
        return self._summary

    def load_full_fields(self, trade_date: date, codes: tuple[str, ...]) -> HistoricalFullFieldBundle:
        return _bundle(codes)


class _Evaluator:
    def __init__(self, score: float = 60.0) -> None:
        self._score = score

    def evaluate(
        self,
        summary: HistoricalDaySummary,
        bundle: HistoricalFullFieldBundle,
    ) -> tuple[HistoricalEvaluatedCandidate, ...]:
        by_code = {item.code: item for item in summary.candidates}
        return tuple(
            HistoricalEvaluatedCandidate(
                code,
                by_code[code].board,
                by_code[code].industry,
                self._score,
                self._score,
                by_code[code].eligible_pools,
            )
            for code in bundle.requested_codes
        )


class _CoverageOnlyPort(_Port):
    def __init__(self) -> None:
        super().__init__()
        self.requested: list[date] = []

    def is_trading_day(self, trade_date: date) -> bool:
        return trade_date.weekday() < 5 and trade_date != date(2026, 6, 19)

    def read_day_summary(self, trade_date: date) -> HistoricalDaySummary:
        self.requested.append(trade_date)
        raise LookupError("fixture has no historical point-in-time epoch")


class _WindowPort(_Port):
    def __init__(self, failed: set[date] | None = None) -> None:
        super().__init__()
        self.failed = failed or set()
        self.requested: list[date] = []

    def is_trading_day(self, trade_date: date) -> bool:
        return trade_date.weekday() < 5 and trade_date != date(2026, 6, 19)

    def read_day_summary(self, trade_date: date) -> HistoricalDaySummary:
        self.requested.append(trade_date)
        if trade_date in self.failed:
            raise LookupError("missing point-in-time fixture")
        return _shift_summary(_summary(), trade_date)

    def load_full_fields(self, trade_date: date, codes: tuple[str, ...]) -> HistoricalFullFieldBundle:
        return _shift_bundle(_bundle(codes), trade_date)


def _shift_summary(summary: HistoricalDaySummary, trade_date: date) -> HistoricalDaySummary:
    days = (trade_date - summary.trade_date).days
    return replace(
        summary,
        trade_date=trade_date,
        observed_at=summary.observed_at + timedelta(days=days),
        candidates=tuple(
            replace(
                candidate,
                feature_as_of=candidate.feature_as_of + timedelta(days=days),
                lineage=_shift_lineage(candidate.lineage, days),
            )
            for candidate in summary.candidates
        ),
    )


def _shift_bundle(bundle: HistoricalFullFieldBundle, trade_date: date) -> HistoricalFullFieldBundle:
    days = (trade_date - bundle.trade_date).days
    return replace(
        bundle,
        trade_date=trade_date,
        candidates=tuple(
            replace(
                candidate,
                feature_as_of=candidate.feature_as_of + timedelta(days=days),
                lineage=_shift_lineage(candidate.lineage, days),
            )
            for candidate in bundle.candidates
        ),
        daily_bars=tuple(
            replace(
                bar,
                session_date=bar.session_date + timedelta(days=days),
                lineage=_shift_lineage(bar.lineage, days),
            )
            for bar in bundle.daily_bars
        ),
        minute_bars=tuple(
            replace(
                bar,
                minute=bar.minute + timedelta(days=days),
                lineage=_shift_lineage(bar.lineage, days),
            )
            for bar in bundle.minute_bars
        ),
        adjustment_windows=tuple(
            replace(
                window,
                as_of=window.as_of + timedelta(days=days),
                factors=tuple((factor_date + timedelta(days=days), value) for factor_date, value in window.factors),
                lineage=_shift_lineage(window.lineage, days),
            )
            for window in bundle.adjustment_windows
        ),
        settlements=tuple(
            replace(
                settlement,
                basis=replace(
                    settlement.basis,
                    freeze_date=settlement.basis.freeze_date + timedelta(days=days),
                    label_date=settlement.basis.label_date + timedelta(days=days),
                ),
                lineage=_shift_lineage(settlement.lineage, days),
            )
            for settlement in bundle.settlements
        ),
    )


def _shift_lineage(lineage: ResearchDataLineage, days: int) -> ResearchDataLineage:
    return replace(
        lineage,
        source_time=lineage.source_time + timedelta(days=days),
        received_at=lineage.received_at + timedelta(days=days),
    )

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

from tests.unit.application.research.test_historical_ports import TRADE_DATE, _bundle, _summary
from trader.application.ports.market import MarketDataPlaneSnapshot
from trader.application.research.extraction import ScoreR2HistoricalExtractor
from trader.application.research.models import (
    HistoricalDaySummary,
    HistoricalEvaluatedCandidate,
    HistoricalFullFieldBundle,
)
from trader.domain.research.historical import ResearchDataLineage, ScoreComponent


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


def test_extractor_is_deterministic_and_keeps_true_coverage_status() -> None:
    first = ScoreR2HistoricalExtractor(_Port(), _Evaluator()).extract()
    second = ScoreR2HistoricalExtractor(_Port(), _Evaluator()).extract()

    assert first.status == "exploratory"
    assert first.content_hash == second.content_hash
    assert tuple(item.summary.trade_date for item in first.days) == (TRADE_DATE,)
    assert first.coverage[-1].trade_date == TRADE_DATE
    assert first.coverage[-1].status == "valid"


def test_active_set_starts_from_each_board_top120_and_proves_exclusions() -> None:
    summary = _summary()
    candidates = tuple(
        replace(candidate, production_top120=candidate.code != "600001") for candidate in summary.candidates
    )
    result = ScoreR2HistoricalExtractor(_Port(replace(summary, candidates=candidates)), _Evaluator()).extract()
    day = result.days[0]

    assert day.full_fields.requested_codes == ("300001", "600001", "688001")
    assert {proof.status for proof in day.proofs} == {"loaded"}
    assert all(len(proof.content_hash) == 64 for proof in day.proofs)


def test_active_set_excludes_candidate_blocked_by_selection_constraint() -> None:
    summary = _summary()
    candidates = tuple(
        replace(
            candidate,
            production_top120=candidate.code != "600001",
            final_components=(ScoreComponent("final", 1.0, 40.0),)
            if candidate.code == "600001"
            else candidate.final_components,
        )
        for candidate in summary.candidates
    )
    result = ScoreR2HistoricalExtractor(_Port(replace(summary, candidates=candidates)), _Evaluator()).extract()
    day = result.days[0]

    assert day.full_fields.requested_codes == ("300001", "688001")
    excluded = tuple(proof for proof in day.proofs if proof.code == "600001")
    assert {proof.status for proof in excluded} == {"excluded"}
    assert {proof.reason for proof in excluded} == {"selection_constraint"}


class _CoverageOnlyPort(_Port):
    def __init__(self) -> None:
        super().__init__()
        self.requested: list[date] = []

    def is_trading_day(self, trade_date: date) -> bool:
        return trade_date.weekday() < 5 and trade_date != date(2026, 6, 19)

    def read_day_summary(self, trade_date: date) -> HistoricalDaySummary:
        self.requested.append(trade_date)
        raise LookupError("fixture has no historical point-in-time epoch")


def test_missing_main_window_extends_back_only_to_preregistered_lower_bound() -> None:
    port = _CoverageOnlyPort()

    result = ScoreR2HistoricalExtractor(port, _Evaluator()).extract()

    assert result.status == "exploratory"
    assert result.days == ()
    assert date(2026, 6, 19) not in port.requested
    assert min(port.requested) == date(2026, 5, 18)
    assert max(port.requested) == date(2026, 8, 10)
    assert all(item.status == "failed" and item.reason == "point_in_time_data_missing" for item in result.coverage)


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


def test_extractor_stops_at_40_main_window_days_without_fallback() -> None:
    port = _WindowPort()

    result = ScoreR2HistoricalExtractor(port, _Evaluator()).extract()

    assert result.status == "extracted"
    assert len(result.days) == 40
    assert min(port.requested) == date(2026, 6, 15)
    assert date(2026, 6, 19) not in port.requested


def test_invalid_main_day_is_retained_and_replaced_only_by_nearest_prior_day() -> None:
    failed = date(2026, 7, 1)
    port = _WindowPort({failed})

    result = ScoreR2HistoricalExtractor(port, _Evaluator()).extract()

    assert result.status == "extracted"
    assert len(result.days) == 40
    assert min(day.summary.trade_date for day in result.days) == date(2026, 6, 12)
    assert any(item.trade_date == failed and item.status == "failed" for item in result.coverage)


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
                    decision_date=settlement.basis.decision_date + timedelta(days=days),
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

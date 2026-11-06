from __future__ import annotations
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from trader.recommendation.domain.market.data_plane import MarketDataPlaneSnapshot
from trader.training.application.historical_extraction_models import (
    AdjustmentFactorWindow,
    BoardPointInTimeCoverage,
    HardFilterAggregate,
    HistoricalDailyBar,
    HistoricalDaySummary,
    HistoricalFullCandidate,
    HistoricalFullFieldBundle,
    HistoricalMinuteBar,
    HistoricalSettlementEvidence,
)
from trader.training.application.research_ports import HistoricalDataPlaneReadPort
from trader.training.domain.evaluation.historical import (
    CostSettlementBasis,
    HistoricalCandidateSummary,
    ResearchDataLineage,
    ScoreComponent,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")

TRADE_DATE = date(2026, 8, 10)

OBSERVED_AT = datetime(2026, 8, 10, 15, 0, tzinfo=SHANGHAI)


def _lineage(version: str, at: datetime = OBSERVED_AT) -> ResearchDataLineage:
    return ResearchDataLineage(
        source="unified_data_plane",
        source_time=at,
        received_at=at,
        quality_status="complete",
        content_version=version,
        content_hash="a" * 64,
    )


def _candidate(code: str, board: str) -> HistoricalCandidateSummary:
    return HistoricalCandidateSummary(
        code=code,
        board=board,
        feature_as_of=OBSERVED_AT,
        lineage=_lineage(f"candidate-{code}"),
        candidate_components=(ScoreComponent("candidate", 1.0, 80.0),),
        final_components=(ScoreComponent("final", 1.0, None),),
        production_candidate_score=80.0,
        production_top120=True,
    )


def _summary() -> HistoricalDaySummary:
    return HistoricalDaySummary(
        trade_date=TRADE_DATE,
        observed_at=OBSERVED_AT,
        daily_feature_pack_version="daily:2026-08-10:1",
        market_epoch_version="market:2026-08-10:1",
        candidate_quote_epoch_version="candidate:2026-08-10:1",
        research_epoch_version="research:2026-08-10:1",
        input_hash="a" * 64,
        config_version="config-current",
        calendar_version="calendar-current",
        rule_versions=("rules-current",),
        candidates=(
            _candidate("688001", "star"),
            _candidate("600001", "main"),
            _candidate("300001", "chinext"),
        ),
        hard_filter_aggregates=(HardFilterAggregate("main", "st_or_delisting", 2),),
        board_coverages=tuple(BoardPointInTimeCoverage(board, True, True) for board in ("main", "chinext", "star")),
        source_versions=(("market", "market-initial"), ("daily", "daily-current")),
    )


def _bundle(codes: tuple[str, ...]) -> HistoricalFullFieldBundle:
    board_by_code = {"600001": "main", "300001": "chinext", "688001": "star"}
    candidates = tuple(
        HistoricalFullCandidate(
            code,
            board_by_code[code],
            OBSERVED_AT,
            {"feature": 80.0},
            _lineage(f"full-{code}"),
        )
        for code in codes
    )
    daily = tuple(
        HistoricalDailyBar(
            code=code,
            session_date=TRADE_DATE - timedelta(days=1),
            open_price=10.0,
            high=11.0,
            low=9.0,
            close=10.5,
            volume=1000.0,
            amount=10_000.0,
            adjustment_window_id=f"window-{code}",
            lineage=_lineage("daily-current", OBSERVED_AT - timedelta(days=1)),
        )
        for code in codes
    )
    minute = tuple(
        HistoricalMinuteBar(
            code=code,
            minute=OBSERVED_AT - timedelta(minutes=1),
            close=10.5,
            volume=100.0,
            amount=1000.0,
            lineage=_lineage("minute-fixture", OBSERVED_AT - timedelta(minutes=1)),
        )
        for code in codes
    )
    windows = tuple(
        AdjustmentFactorWindow(
            window_id=f"window-{code}",
            code=code,
            as_of=OBSERVED_AT - timedelta(days=1),
            factors=((TRADE_DATE - timedelta(days=1), 1.0),),
            lineage=_lineage("factor-fixture", OBSERVED_AT - timedelta(days=1)),
        )
        for code in codes
    )
    settlements = tuple(
        HistoricalSettlementEvidence(
            basis=CostSettlementBasis(
                code,
                board_by_code[code],
                TRADE_DATE,
                TRADE_DATE + timedelta(days=1),
                0.03,
                -0.5,
                0.2,
            ),
            lineage=_lineage("settlement-fixture", OBSERVED_AT + timedelta(days=1)),
        )
        for code in codes
    )
    return HistoricalFullFieldBundle(
        TRADE_DATE,
        "a" * 64,
        codes,
        candidates,
        daily,
        minute,
        windows,
        settlements,
        ("main", "chinext", "star"),
    )


class _PortDouble:
    def snapshot(self) -> MarketDataPlaneSnapshot:
        return MarketDataPlaneSnapshot(None, None, None, None)

    def is_trading_day(self, trade_date: date) -> bool:
        return trade_date == TRADE_DATE

    def read_day_summary(self, trade_date: date) -> HistoricalDaySummary:
        assert trade_date == TRADE_DATE
        return _summary()

    def load_full_fields(self, trade_date: date, codes: tuple[str, ...]) -> HistoricalFullFieldBundle:
        assert trade_date == TRADE_DATE
        return _bundle(codes)


def _consume_port(port: HistoricalDataPlaneReadPort) -> tuple[str, ...]:
    summary = port.read_day_summary(TRADE_DATE)
    codes = tuple(item.code for item in summary.candidates)
    return port.load_full_fields(TRADE_DATE, codes).requested_codes

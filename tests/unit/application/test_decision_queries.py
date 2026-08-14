from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

from trader.application.decision_core import UnifiedDecisionIndex
from trader.application.decision_queries import UnifiedDecisionQueries
from trader.domain.recommendation.decision_identity import (
    CommittedDecisionRecord,
    DecisionItem,
    LongProjection,
    LongProjectionItem,
    ScoredDecision,
)
from trader.domain.recommendation.models import RecommendationAction, Strategy

SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 11, 10, 30, tzinfo=SHANGHAI)
TRADE_DATE = NOW.date()


class _Clock:
    def now(self) -> datetime:
        return NOW


class _Repository:
    def __init__(self, record: CommittedDecisionRecord | None = None) -> None:
        self.record = record

    def load(self, strategy: Strategy, trade_date: date) -> CommittedDecisionRecord | None:
        if self.record is not None and self.record.strategy is strategy and self.record.trade_date == trade_date:
            return self.record
        return None

    def list_dates(self, strategy: Strategy, *, limit: int = 31) -> tuple[date, ...]:
        if self.record is not None and self.record.strategy is strategy:
            return (self.record.trade_date,)[:limit]
        return ()


def test_queries_expose_one_shape_for_scored_and_long_current_views() -> None:
    index = UnifiedDecisionIndex()
    scored = _decision()
    long = LongProjection(
        NOW.date(),
        1,
        NOW,
        (("quotes", "quotes:1"),),
        (
            LongProjectionItem(
                "600001",
                "semiconductor",
                "quote:1",
                "样例",
                "设备",
                12.3,
                1.2,
                source="fixture",
                source_time=NOW,
                quote_status="live",
            ),
        ),
    )
    assert index.publish(scored, expected_version=None).accepted
    assert index.publish(long, expected_version=None).accepted
    queries = UnifiedDecisionQueries(index, _Repository(), _Clock())

    today = queries.current(Strategy.TODAY)
    long_view = queries.current(Strategy.LONG)

    assert today.status == "ready"
    assert today.strategy is Strategy.TODAY
    assert today.score_status == "scored"
    assert today.coverage.selected_count == 1
    assert today.items[0].name == "浦发银行"
    assert today.items[0].industry == "银行"
    assert today.items[0].final_score == 84.0
    assert long_view.status == "ready"
    assert long_view.strategy is Strategy.LONG
    assert long_view.score_status == "not_applicable"
    assert long_view.items[0].name == "样例"
    assert long_view.items[0].price == 12.3


def test_queries_read_only_formal_history_and_long_has_no_history() -> None:
    decision = _decision(trade_date=date(2026, 8, 8))
    record = CommittedDecisionRecord(
        decision,
        datetime(2026, 8, 8, 11, 20, tzinfo=SHANGHAI),
        "scheduled",
    )
    queries = UnifiedDecisionQueries(UnifiedDecisionIndex(), _Repository(record), _Clock())

    history = queries.history(Strategy.TODAY, date(2026, 8, 8))
    long_history = queries.history(Strategy.LONG, date(2026, 8, 8))

    assert history.status == "ready"
    assert history.frozen is True
    assert history.freeze_kind == "scheduled"
    assert queries.dates(Strategy.TODAY) == (date(2026, 8, 8),)
    assert long_history.status == "not_applicable"
    assert long_history.degraded_reasons == ("history_not_applicable",)
    assert queries.dates(Strategy.LONG) == ()


def test_scored_coverage_uses_distinct_evaluation_counts_not_overlapping_reasons() -> None:
    index = UnifiedDecisionIndex()
    scored = replace(_decision(), population_count=82, rejected_count=81)
    assert index.publish(scored, expected_version=None).accepted

    view = UnifiedDecisionQueries(index, _Repository(), _Clock()).current(Strategy.TODAY)

    assert view.coverage.candidate_count == 82
    assert view.coverage.evaluated_count == 1
    assert view.coverage.rejected_count == 81
    assert dict(view.filter_reason_counts) == {"hard_filter": 10}


def _decision(*, trade_date: date = TRADE_DATE) -> ScoredDecision:
    return ScoredDecision(
        Strategy.TODAY,
        trade_date,
        1,
        datetime.combine(trade_date, NOW.timetz()),
        "local",
        None,
        (("market", "market:1"),),
        "config:1",
        "strategy:1",
        "fusion:1",
        (
            DecisionItem(
                "600000",
                RecommendationAction.EXECUTABLE,
                True,
                1,
                88.0,
                84.0,
                84.0,
                (("local_score", 84.0),),
                ("risk_example",),
                "threshold_met",
                "浦发银行",
                "银行",
            ),
        ),
        (("hard_filter", 10),),
    )

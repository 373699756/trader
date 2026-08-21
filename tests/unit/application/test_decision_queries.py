from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

from trader.application.decision_core import UnifiedDecisionIndex
from trader.application.decision_queries import UnifiedDecisionQueries
from trader.domain.recommendation.decision_identity import (
    CommittedDecisionRecord,
    DecisionItem,
    DecisionOverlay,
    DecisionQuote,
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
    assert today.items[0].price == 10.25
    assert today.items[0].pct_change == 2.5
    assert today.items[0].amount == 1_000_000_000.0
    assert today.items[0].turnover_rate == 0.8
    assert today.items[0].market_cap == 300_000_000_000.0
    assert today.items[0].quote_source == "fixture"
    assert today.items[0].quote_status == "decision_anchor"
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
    assert history.items[0].price == 10.25
    assert history.items[0].amount == 1_000_000_000.0
    assert history.items[0].turnover_rate == 0.8
    assert history.items[0].market_cap == 300_000_000_000.0
    assert history.items[0].quote_status == "decision_anchor"
    assert queries.dates(Strategy.TODAY) == (date(2026, 8, 8),)
    assert long_history.status == "not_applicable"
    assert long_history.degraded_reasons == ("history_not_applicable",)
    assert queries.dates(Strategy.LONG) == ()


def test_current_overlay_replaces_every_quote_field_without_changing_decision_identity() -> None:
    index = UnifiedDecisionIndex()
    scored = _decision()
    assert index.publish(scored, expected_version=None).accepted
    overlay_quote = DecisionQuote(
        "600000",
        10.5,
        3.0,
        1_100_000_000.0,
        0.9,
        310_000_000_000.0,
        "tencent",
        NOW,
        "quote:2",
    )
    overlay = DecisionOverlay(scored.strategy, scored.trade_date, scored.version, NOW, (overlay_quote,))
    assert index.publish_overlay(overlay, expected_version=None).accepted

    view = UnifiedDecisionQueries(index, _Repository(), _Clock()).current(Strategy.TODAY)

    assert view.decision_version == scored.version
    assert view.items[0].price == 10.5
    assert view.items[0].pct_change == 3.0
    assert view.items[0].amount == 1_100_000_000.0
    assert view.items[0].turnover_rate == 0.9
    assert view.items[0].market_cap == 310_000_000_000.0
    assert view.items[0].quote_source == "tencent"
    assert view.items[0].quote_status == "live"


def test_scored_coverage_uses_distinct_evaluation_counts_not_overlapping_reasons() -> None:
    index = UnifiedDecisionIndex()
    scored = replace(_decision(), population_count=82, rejected_count=81)
    assert index.publish(scored, expected_version=None).accepted

    view = UnifiedDecisionQueries(index, _Repository(), _Clock()).current(Strategy.TODAY)

    assert view.coverage.candidate_count == 82
    assert view.coverage.evaluated_count == 1
    assert view.coverage.rejected_count == 81
    assert dict(view.filter_reason_counts) == {"hard_filter": 10}


def test_scored_query_restores_rank_order_from_code_sorted_identity() -> None:
    index = UnifiedDecisionIndex()
    decision = replace(
        _decision(),
        items=(
            _item("600003", RecommendationAction.OBSERVE, rank=4, final_score=73.0),
            _item("600001", RecommendationAction.OBSERVE, rank=3, final_score=75.0),
            _item("600004", RecommendationAction.EXECUTABLE, rank=2, final_score=81.0),
            _item("600002", RecommendationAction.EXECUTABLE, rank=1, final_score=86.0),
        ),
    )
    assert tuple(item.code for item in decision.items) == ("600001", "600002", "600003", "600004")
    assert index.publish(decision, expected_version=None).accepted

    view = UnifiedDecisionQueries(index, _Repository(), _Clock()).current(Strategy.TODAY)

    assert [(item.code, item.rank, item.final_score) for item in view.items] == [
        ("600002", 1, 86.0),
        ("600004", 2, 81.0),
        ("600001", 3, 75.0),
        ("600003", 4, 73.0),
    ]


def _item(
    code: str,
    action: RecommendationAction,
    *,
    rank: int,
    final_score: float,
) -> DecisionItem:
    return DecisionItem(
        code,
        action,
        True,
        rank,
        final_score,
        final_score,
        final_score,
        (("local_score", final_score),),
        (),
        "threshold_met" if action is RecommendationAction.EXECUTABLE else "near_score_threshold",
        f"样例{code}",
        "样例行业",
        DecisionQuote(
            code,
            10.0,
            1.0,
            100_000_000.0,
            1.0,
            10_000_000_000.0,
            "fixture",
            NOW,
            f"quote:{code}",
        ),
    )


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
                DecisionQuote(
                    "600000",
                    10.25,
                    2.5,
                    1_000_000_000.0,
                    0.8,
                    300_000_000_000.0,
                    "fixture",
                    datetime.combine(trade_date, NOW.timetz()),
                    "quote:1",
                ),
            ),
        ),
        (("hard_filter", 10),),
    )

import pandas as pd

from stock_analyzer.long_term_watch import LongTermWatchScorer
from stock_analyzer.strategies.today_policies import IndustryDiversificationPolicy, TodayExecutionWindowPolicy


def test_long_term_watch_selects_low_valuation_strategic_leader_from_candidate_pool():
    candidates = pd.DataFrame(
        [
            {
                "code": "002371",
                "name": "北方华创",
                "industry": "半导体设备 国产替代",
                "market_cap": 1200e8,
                "fundamental_value_score": 68,
                "fundamental_quality_score": 74,
                "roe": 16,
                "revenue_yoy": 20,
                "net_profit_yoy": 25,
                "sixty_day_pct": 12,
                "ytd_pct": 18,
            },
            {
                "code": "600001",
                "name": "普通高估热门",
                "industry": "普通消费",
                "market_cap": 200e8,
                "fundamental_value_score": 20,
                "fundamental_quality_score": 72,
                "pe_dynamic": 160,
                "pb": 20,
                "sixty_day_pct": 120,
                "ytd_pct": 220,
            },
        ]
    )

    rows = LongTermWatchScorer().score({}, candidates, top_n=5)

    assert [row["code"] for row in rows] == ["002371"]
    profile = rows[0]["long_term_profile"]
    assert profile["valuation_score"] >= 0.55
    assert profile["leader_score"] == 1.0
    assert profile["strategic_score"] >= 0.45
    assert rows[0]["execution_allowed"] is False
    assert rows[0]["trade_action"]["position_size"] == 0.0
    assert rows[0]["expected_return_net"] is None
    assert rows[0]["predicted_net_return"] is None
    assert rows[0]["ranking_source"] == "long_term_composite_score"


def test_long_term_watch_does_not_require_keyword_when_builtin_leader_has_strategic_segment():
    candidates = pd.DataFrame(
        [
            {
                "code": "688012",
                "name": "中微公司",
                "industry": "设备",
                "market_cap": 900e8,
                "fundamental_value_score": 64,
                "fundamental_quality_score": 76,
                "roe": 14,
                "revenue_yoy": 16,
                "net_profit_yoy": 18,
                "sixty_day_pct": 8,
            },
        ]
    )

    rows = LongTermWatchScorer().score({}, candidates, top_n=5)

    assert rows
    assert rows[0]["code"] == "688012"
    assert rows[0]["long_term_profile"]["leader_reason"] == "内置战略产业龙头名单"


def test_long_term_watch_keeps_missing_fundamentals_neutral_not_high_score():
    candidates = pd.DataFrame(
        [
            {
                "code": "600001",
                "name": "缺失基本面半导体",
                "industry": "半导体 国产替代",
                "market_cap": 100e8,
            },
        ]
    )

    rows = LongTermWatchScorer().score({}, candidates, top_n=5)

    assert rows == []


def test_today_execution_window_policy_marks_only_buy_window_immediate():
    policy = TodayExecutionWindowPolicy(start="09:36", end="14:00")

    assert policy.state(pd.Timestamp("2026-07-15 09:35").to_pydatetime())[1] == "open_observe"
    assert policy.state(pd.Timestamp("2026-07-15 09:36").to_pydatetime())[1] == "main_execution"
    assert policy.state(pd.Timestamp("2026-07-15 10:30").to_pydatetime())[1] == "late_execution"
    assert policy.state(pd.Timestamp("2026-07-15 14:00").to_pydatetime())[1] == "afternoon_observe"
    assert policy.state(pd.Timestamp("2026-07-15 14:01").to_pydatetime())[1] == "backup_only"


def test_industry_diversification_policy_preserves_order_and_caps_industry():
    rows = [
        {"code": "600001", "industry": "半导体"},
        {"code": "600002", "industry": "半导体"},
        {"code": "600003", "industry": "半导体"},
        {"code": "600004", "industry": "电力"},
    ]

    selected, distribution, limited = IndustryDiversificationPolicy().select(rows, limit=4, cap=2)

    assert [row["code"] for row in selected] == ["600001", "600002", "600004"]
    assert distribution == {"半导体": 2, "电力": 1}
    assert limited == 1

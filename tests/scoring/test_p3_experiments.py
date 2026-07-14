import pandas as pd

from stock_analyzer.p3_experiments import (
    MAX_EXIT_CANDIDATES,
    evaluate_exit_policy_candidates,
    exit_policy_candidates,
)


def test_p3_exit_policy_candidates_are_preregistered_and_bounded():
    tomorrow = exit_policy_candidates("tomorrow_picks")
    swing = exit_policy_candidates("swing_picks")

    assert 3 <= len(tomorrow) <= MAX_EXIT_CANDIDATES
    assert 3 <= len(swing) <= MAX_EXIT_CANDIDATES
    assert tomorrow[0]["candidate_id"] == "current_8_5_4"
    assert swing[0]["candidate_id"] == "current_8_5_4"


def test_p3_exit_policy_evaluation_uses_conservative_stop_first_order():
    samples = [
        {
            "signal_date": "2024-01-02",
            "entry_price": 100.0,
            "raw_prices": pd.DataFrame(
                [
                    {
                        "trade_date": "20240103",
                        "open": 100.0,
                        "high": 110.0,
                        "low": 94.0,
                        "price": 101.0,
                    }
                ]
            ),
        }
    ]

    result = evaluate_exit_policy_candidates("tomorrow_picks", samples)
    current = next(item for item in result["results"] if item["candidate_id"] == "current_8_5_4")

    assert result["candidate_count"] <= MAX_EXIT_CANDIDATES
    assert result["multiple_testing_trials"] == result["candidate_count"]
    assert result["conservative_intraday_order"] == "stop_first_when_daily_bar_hits_stop_and_take_profit"
    assert current["status"] == "ok"
    assert current["avg_portfolio_return"] == -5.0

from datetime import datetime
from unittest.mock import patch

import pandas as pd

from stock_analyzer import config
from stock_analyzer.execution_policy import build_execution_policy
from stock_analyzer.point_in_time import build_candidate_snapshot_rows
from stock_analyzer.scoring import prepare_candidates
from stock_analyzer.snapshot import run_snapshot
from stock_analyzer.strategy_validation import StrategyValidationStore, validation_baseline_config


def _quotes(count=35):
    frame = pd.DataFrame(
        [
            {
                "code": "600{:03d}".format(index),
                "name": "样本{}".format(index),
                "price": 10.0 + index * 0.01,
                "open": 9.9 + index * 0.01,
                "high": 10.2 + index * 0.01,
                "low": 9.8 + index * 0.01,
                "pct_chg": 2.0,
                "turnover": 500_000_000,
                "volume": 1_000_000,
                "volume_ratio": 1.5,
                "turnover_rate": 3.0,
                "industry": "行业A" if index % 2 else "行业B",
                "market_cap": 10_000_000_000 + index * 5_000_000_000,
                "sixty_day_pct": 8.0,
                "ytd_pct": 12.0,
            }
            for index in range(count)
        ]
    )
    frame.attrs["quote_timestamp"] = "2024-01-01T15:00:00"
    return frame


def _history():
    return pd.DataFrame(
        [
            {"trade_date": "20240101", "open": 10.0, "high": 10.2, "low": 9.9, "price": 10.0},
            {"trade_date": "20240102", "open": 10.0, "high": 10.4, "low": 9.9, "price": 10.3},
            {"trade_date": "20240103", "open": 10.3, "high": 10.5, "low": 10.1, "price": 10.4},
            {"trade_date": "20240104", "open": 10.4, "high": 10.6, "low": 10.2, "price": 10.5},
            {"trade_date": "20240105", "open": 10.5, "high": 10.7, "low": 10.3, "price": 10.6},
            {"trade_date": "20240108", "open": 10.6, "high": 10.8, "low": 10.4, "price": 10.7},
        ]
    )


def test_snapshot_persists_full_evaluated_pool_not_only_top_k(tmp_path):
    quotes = _quotes(35)
    selected = [
        {"rank": index + 1, "code": row["code"], "name": row["name"], "price": row["price"], "score": 90 - index}
        for index, row in quotes.head(2).iterrows()
    ]

    class Provider:
        def get_realtime_quotes(self):
            return quotes

        def health(self):
            return {"quotes_source": "测试源", "last_quote_refresh": "2024-01-01T15:00:00"}

    store = StrategyValidationStore(str(tmp_path / "validation.sqlite3"))
    meta = {"generated_at": "2024-01-01T14:50:00", "top_n": 2}
    with patch.object(config, "QUOTE_SNAPSHOT_MIN_ROWS", 1), patch.object(
        config, "TOMORROW_SIGNAL_CUTOFF_TIME", "23:59"
    ), patch.object(
        config, "ENABLE_HISTORY_FACTORS", False
    ), patch(
        "stock_analyzer.snapshot._quote_freshness_error", return_value=""
    ), patch("stock_analyzer.snapshot._score_snapshot_strategy", return_value=(selected, meta, "pit_v1")), patch(
        "stock_analyzer.snapshot._apply_snapshot_deepseek_rerank", return_value=(selected, {})
    ), patch("stock_analyzer.snapshot._after_close_anchor_time", return_value=False), patch(
        "stock_analyzer.snapshot._signal_at_or_after_tomorrow_cutoff", return_value=False
    ):
        result = run_snapshot(Provider(), store, "tomorrow_picks")

    signal_date = result["meta"]["generated_at"][:10]
    candidates = store.candidate_snapshots_for_date(signal_date, "tomorrow_picks")
    assert result["ok"]
    assert result["saved"]["saved"] == 2
    assert result["saved"]["candidate_saved"] == 35
    assert len(candidates) == 35
    assert sum(row["selected"] for row in candidates) == 2
    assert sum(not row["selected"] for row in candidates) == 33
    assert all("raw_source.price" in row["missing_mask"] for row in candidates)
    assert all(
        row["source_timestamps"]["market_data_cutoff"] == result["meta"]["generated_at"]
        for row in candidates
    )


def test_missing_history_is_unknown_and_never_fabricates_delisting_return(tmp_path):
    class Provider:
        def get_history(self, code, days=180):
            raise RuntimeError("source unavailable")

    store = StrategyValidationStore(str(tmp_path / "validation.sqlite3"))
    store.save_signals(
        "tomorrow_picks",
        "pit_v1",
        "2024-01-01T15:00:00",
        [{"rank": 1, "code": "600001", "name": "未知样本", "price": 10.0, "score": 90}],
    )
    result = store.update_outcomes(Provider(), signal_date="2024-01-01", strategy_name="tomorrow_picks")
    row = store.signals_for_date("2024-01-01", "tomorrow_picks")[0]

    assert result["updated"] == 0
    assert result["unknown"] == 1
    assert row["label_status"] == "unknown"
    assert row["gross_return_pct"] is None
    assert not row["promotion_eligible"]
    assert row["outcome_updated_at"] is None


def test_outcome_uses_policy_frozen_at_signal_time(tmp_path):
    class Provider:
        def get_history(self, code, days=180):
            history = _history().copy()
            history.loc[1, "open"] = 10.4
            history.loc[1, "price"] = 10.5
            return history

    store = StrategyValidationStore(str(tmp_path / "validation.sqlite3"))
    with patch.object(config, "TOMORROW_HIGH_OPEN_SKIP_PCT", 5.0), patch.object(
        config, "VALIDATION_TRADE_COST_PCT", 0.2
    ):
        frozen_policy = build_execution_policy("tomorrow_picks")
    store.save_signals(
        "tomorrow_picks",
        "pit_v1",
        "2024-01-01T15:00:00",
        [{"rank": 1, "code": "600001", "name": "冻结规则", "price": 10.0, "score": 90}],
        execution_policy=frozen_policy,
    )
    with patch.object(config, "TOMORROW_HIGH_OPEN_SKIP_PCT", 1.0), patch.object(
        config, "VALIDATION_TRADE_COST_PCT", 9.0
    ):
        result = store.update_outcomes(Provider(), signal_date="2024-01-01", strategy_name="tomorrow_picks")
        current_baseline_id = validation_baseline_config("tomorrow_picks")["baseline_id"]
    row = store.signals_for_date("2024-01-01", "tomorrow_picks")[0]

    assert result["updated"] == 1
    assert row["execution_policy_version"] == frozen_policy["policy_version"]
    assert row["cost_scenarios"]["base"]["fee_pct"] == 0.2
    assert row["cost_scenarios"]["medium"] == row["cost_scenarios"]["base"]
    assert row["validation_baseline_id"] == validation_baseline_config(
        "tomorrow_picks", execution_policy=frozen_policy
    )["baseline_id"]
    assert row["validation_baseline_id"] != current_baseline_id
    assert frozen_policy["policy_version"].rsplit(".", 1)[-1] in row["validation_baseline_id"]


def test_explicit_delisting_uses_last_tradable_price(tmp_path):
    class Provider:
        def get_security_status(self, code):
            return {"status": "delisted"}

        def get_history(self, code, days=180):
            return pd.DataFrame(
                [
                    {"trade_date": "20240101", "open": 10.0, "high": 10.2, "low": 9.9, "price": 10.0},
                    {"trade_date": "20240102", "open": 10.0, "high": 10.3, "low": 9.9, "price": 10.2},
                    {"trade_date": "20240103", "open": 10.2, "high": 10.6, "low": 10.1, "price": 10.5},
                ]
            )

    store = StrategyValidationStore(str(tmp_path / "validation.sqlite3"))
    store.save_signals(
        "swing_picks",
        "pit_v1",
        "2024-01-01T15:00:00",
        [{"rank": 1, "code": "600001", "name": "退市样本", "price": 10.0, "score": 90}],
    )
    result = store.update_outcomes(Provider(), signal_date="2024-01-01", strategy_name="swing_picks")
    row = store.signals_for_date("2024-01-01", "swing_picks")[0]

    assert result["updated"] == 1
    assert row["delisting_status"] == "liquidated_last_tradable"
    assert row["actual_entry_price"] == 10.0
    assert row["actual_exit_price"] == 10.5
    assert row["gross_return_pct"] == 5.0
    assert row["correction_reason"] == "delisted_last_tradable_liquidation"


def test_unfilled_exit_preserves_entry_and_unfilled_exit_quantities(tmp_path):
    class Provider:
        def get_security_status(self, code):
            return {"status": "delisted"}

        def get_history(self, code, days=180):
            return pd.DataFrame(
                [
                    {"trade_date": "20240101", "open": 10.0, "high": 10.1, "low": 9.9, "price": 10.0},
                    {"trade_date": "20240102", "open": 9.0, "high": 9.05, "low": 9.0, "price": 9.0},
                    {"trade_date": "20240103", "open": 8.1, "high": 8.15, "low": 8.1, "price": 8.1},
                ]
            )

    store = StrategyValidationStore(str(tmp_path / "validation.sqlite3"))
    store.save_signals(
        "swing_picks",
        "pit_v1",
        "2024-01-01T15:00:00",
        [{"rank": 1, "code": "600001", "name": "无法退出", "price": 10.0, "score": 90}],
    )
    result = store.update_outcomes(Provider(), signal_date="2024-01-01", strategy_name="swing_picks")
    row = store.signals_for_date("2024-01-01", "swing_picks")[0]

    assert result["execution_skipped"] == 0
    assert row["label_status"] == "pending"
    assert row["entry_status"] == "filled"
    assert row["exit_status"] == "pending"
    assert row["actual_filled_quantity"] == row["order_quantity"]
    assert row["actual_exit_quantity"] == 0
    assert row["unfilled_entry_quantity"] == 0
    assert row["unfilled_exit_quantity"] == 0
    assert row["gross_return_pct"] is None


def test_future_quote_timestamp_settles_but_is_blocked_from_promotion(tmp_path):
    signal_time = "2024-01-01T15:00:00"
    quotes = _quotes(1)
    quotes.attrs["quote_timestamp"] = "2024-01-01T15:01:00"
    candidates = prepare_candidates(quotes)
    selected = [{**candidates.iloc[0].to_dict(), "rank": 1, "score": 90.0}]
    candidate_rows = build_candidate_snapshot_rows(quotes, candidates, selected, signal_time)

    class Provider:
        def get_history(self, code, days=180):
            return _history()

    store = StrategyValidationStore(str(tmp_path / "validation.sqlite3"))
    policy = build_execution_policy("tomorrow_picks")
    store.save_signals(
        "tomorrow_picks",
        config.TOMORROW_STRATEGY_VERSION,
        signal_time,
        selected,
        candidate_rows=candidate_rows,
        batch_metadata={
            "data_source_timestamp": "2024-01-01T15:01:00",
            "market_data_cutoff": signal_time,
        },
        execution_policy=policy,
    )
    update = store.update_outcomes(Provider(), signal_date="2024-01-01", strategy_name="tomorrow_picks")
    row = store.signals_for_date("2024-01-01", "tomorrow_picks")[0]
    metrics = store.metrics("tomorrow_picks", days=20)

    assert update["updated"] == 1
    assert row["label_status"] == "settled"
    assert not row["promotion_eligible"]
    assert metrics["raw_outcome_sample_count"] == 1
    assert metrics["sample_count"] == 0
    assert metrics["excluded_promotion_ineligible_count"] == 1
    assert store.live_weight_samples("tomorrow_picks", days=20) == []


def test_audit_30_signals_recomputes_returns_and_conserves_samples(tmp_path):
    signal_time = "2024-01-01T15:00:00"
    quotes = _quotes(35)
    candidates = prepare_candidates(quotes)
    selected = [
        {
            **row,
            "rank": index + 1,
            "score": 90.0 - index,
        }
        for index, row in enumerate(candidates.head(30).to_dict(orient="records"))
    ]
    candidate_rows = build_candidate_snapshot_rows(quotes, candidates, selected, signal_time)

    class Provider:
        def get_history(self, code, days=180):
            return _history()

    store = StrategyValidationStore(str(tmp_path / "validation.sqlite3"))
    store.save_signals(
        "tomorrow_picks",
        "pit_v1",
        signal_time,
        selected,
        candidate_rows=candidate_rows,
        batch_metadata={"data_source_timestamp": signal_time, "market_data_cutoff": signal_time},
        execution_policy=build_execution_policy("tomorrow_picks"),
    )
    update = store.update_outcomes(Provider(), signal_date="2024-01-01", strategy_name="tomorrow_picks")
    audit = store.audit_point_in_time("tomorrow_picks", sample_size=30)

    assert update["updated"] == 30
    assert audit["ok"], audit["violations"]
    assert audit["sample_count"] == 30
    assert audit["point_in_time_valid_count"] == 30
    assert audit["return_reproducible_count"] == 30
    assert audit["cost_reproducible_count"] == 30
    assert audit["execution_policy_valid_count"] == 30
    assert audit["label_status_counts"] == {"settled": 30}
    assert all(item["conserved"] for item in audit["batch_conservation"])

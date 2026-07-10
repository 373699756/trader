import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from stock_analyzer import config
from stock_analyzer.strategy_validation import StrategyValidationStore


class StrategyValidationDeepSeekAttributionTest(unittest.TestCase):
    def test_deepseek_attribution_compares_local_and_reranked_topn(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "validation.sqlite3")
            attribution_path = os.path.join(tmpdir, "deepseek_attribution.json")
            with patch.object(config, "DEEPSEEK_ATTRIBUTION_PATH", attribution_path), patch.object(
                config, "TOMORROW_PRIMARY_WATCH_N", 1
            ), patch.object(config, "VALIDATION_TRADE_COST_PCT", 0.0), patch.object(
                config, "VALIDATION_SLIPPAGE_HIGH_TURNOVER_PCT", 0.0
            ), patch.object(
                config, "VALIDATION_SLIPPAGE_MID_TURNOVER_PCT", 0.0
            ), patch.object(
                config, "VALIDATION_SLIPPAGE_LOW_TURNOVER_PCT", 0.0
            ), patch.object(
                config, "VALIDATION_SLIPPAGE_MICRO_TURNOVER_PCT", 0.0
            ):
                store = StrategyValidationStore(db_path)
                store.save_signals(
                    "tomorrow_picks",
                    config.TOMORROW_STRATEGY_VERSION,
                    "2026-01-02T15:01:00",
                    [
                        _row("000001", 1, 2, "priority"),
                        _row("000002", 2, 1, "watch"),
                    ],
                )
                store.save_signals(
                    "tomorrow_picks",
                    config.TOMORROW_STRATEGY_VERSION,
                    "2026-01-03T15:01:00",
                    [
                        _row("000003", 1, 1, "watch"),
                        _row("000004", 2, 2, "priority"),
                    ],
                )
                _insert_outcomes(db_path, {"000001": 4.0, "000002": -2.0, "000003": 1.0, "000004": 3.0})

                attribution = store.deepseek_attribution("tomorrow_picks", days=20)

        self.assertEqual(attribution["status"], "insufficient_real_samples")
        self.assertEqual(attribution["sample_count"], 4)
        self.assertEqual(attribution["real_sample_count"], 4)
        self.assertEqual(attribution["covered_sample_count"], 4)
        self.assertEqual(attribution["reordered_sample_count"], 2)
        self.assertEqual(attribution["priority_vs_watch"]["win_rate_delta_pct"], 50.0)
        self.assertEqual(attribution["counterfactual_topn"]["top_n"], 1)
        self.assertEqual(attribution["counterfactual_topn"]["sample_count"], 2)
        self.assertEqual(attribution["counterfactual_topn"]["local_avg_primary_return_net"], -0.5)
        self.assertEqual(attribution["counterfactual_topn"]["deepseek_avg_primary_return_net"], 2.5)
        self.assertEqual(attribution["counterfactual_topn"]["avg_return_delta_pct"], 3.0)
        self.assertEqual(attribution["counterfactual_topn"]["win_rate_delta_pct"], 50.0)

    def test_deepseek_attribution_includes_gate_filtered_shadow_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "validation.sqlite3")
            attribution_path = os.path.join(tmpdir, "deepseek_attribution.json")
            with patch.object(config, "DEEPSEEK_ATTRIBUTION_PATH", attribution_path), patch.object(
                config, "TOMORROW_PRIMARY_WATCH_N", 1
            ), patch.object(config, "VALIDATION_TRADE_COST_PCT", 0.0), patch.object(
                config, "VALIDATION_SLIPPAGE_HIGH_TURNOVER_PCT", 0.0
            ), patch.object(
                config, "VALIDATION_SLIPPAGE_MID_TURNOVER_PCT", 0.0
            ), patch.object(
                config, "VALIDATION_SLIPPAGE_LOW_TURNOVER_PCT", 0.0
            ), patch.object(
                config, "VALIDATION_SLIPPAGE_MICRO_TURNOVER_PCT", 0.0
            ):
                store = StrategyValidationStore(db_path)
                saved = store.save_signals(
                    "tomorrow_picks",
                    config.TOMORROW_STRATEGY_VERSION,
                    "2026-01-02T15:01:00",
                    [_row("000001", 1, 2, "priority")],
                    deepseek_shadow_rows=[
                        {
                            **_row("000002", 2, 1, "avoid"),
                            "deepseek_veto": True,
                            "deepseek_penalty": 30,
                            "deepseek_filter_reason": "deepseek_veto",
                        }
                    ],
                )
                update = store.update_outcomes(_FakeProvider(), signal_date="2026-01-02", strategy_name="tomorrow_picks")

                attribution = store.deepseek_attribution("tomorrow_picks", days=20)

        self.assertEqual(saved["deepseek_shadow_saved"], 1)
        self.assertEqual(update["updated"], 1)
        self.assertEqual(update["deepseek_shadow_updated"], 1)
        self.assertEqual(attribution["sample_count"], 2)
        self.assertEqual(attribution["selected_sample_count"], 1)
        self.assertEqual(attribution["shadow_sample_count"], 1)
        self.assertEqual(attribution["avoid_veto"]["sample_count"], 1)
        self.assertEqual(attribution["avoid_veto"]["avg_primary_return_net"], -4.0)
        self.assertEqual(attribution["shadow_avoid_veto"]["avg_primary_return_net"], -4.0)
        self.assertEqual(attribution["counterfactual_topn"]["local_avg_primary_return_net"], -4.0)
        self.assertEqual(attribution["counterfactual_topn"]["deepseek_avg_primary_return_net"], 2.0)
        self.assertEqual(attribution["counterfactual_topn"]["avg_return_delta_pct"], 6.0)

    def test_market_gate_metrics_scores_review_against_outcomes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "validation.sqlite3")
            with patch.object(config, "VALIDATION_TRADE_COST_PCT", 0.0), patch.object(
                config, "VALIDATION_SLIPPAGE_HIGH_TURNOVER_PCT", 0.0
            ), patch.object(
                config, "VALIDATION_SLIPPAGE_MID_TURNOVER_PCT", 0.0
            ), patch.object(
                config, "VALIDATION_SLIPPAGE_LOW_TURNOVER_PCT", 0.0
            ), patch.object(
                config, "VALIDATION_SLIPPAGE_MICRO_TURNOVER_PCT", 0.0
            ):
                store = StrategyValidationStore(db_path)
                store.save_market_gate_review(
                    {
                        "enabled": True,
                        "status": "ok",
                        "regime": "risk_off",
                        "size_factor": 0.4,
                        "confidence": 80,
                        "source": "test",
                        "reason": "宽度弱",
                        "generated_at": "2026-01-02T10:00:00",
                        "context": {"up_ratio_pct": 20},
                        "counts": {"tomorrow_picks": {"before": 5, "after": 2}},
                    }
                )
                store.save_signals(
                    "tomorrow_picks",
                    "tomorrow_picks_v1",
                    "2026-01-02T15:01:00",
                    [_row("000001", 1, 1, "watch")],
                )
                _insert_outcomes(db_path, {"000001": -2.0})

                metrics = store.market_gate_metrics(days=20)

        self.assertEqual(metrics["sample_count"], 1)
        self.assertEqual(metrics["outcome_sample_count"], 1)
        self.assertEqual(metrics["hit_rate"], 100.0)
        self.assertEqual(metrics["by_regime"]["risk_off"]["avg_primary_return_net"], -2.0)
        self.assertTrue(metrics["recent"][0]["hit"])


def _row(code, rank, local_rank, action):
    return {
        "code": code,
        "name": code,
        "rank": rank,
        "price": 10.0,
        "score": 80 - rank,
        "turnover": 1_000_000_000,
        "tier": "primary_watch",
        "local_rank": local_rank,
        "deepseek_covered": True,
        "deepseek_action": action,
        "deepseek_veto": False,
        "deepseek_penalty": 0,
        "deepseek_rank_score": 90 - rank,
        "deepseek_blend_alpha": 0.3,
        "blend_alpha": 0.3,
        "rerank_source": "deepseek",
    }


def _insert_outcomes(db_path, returns_by_code):
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT id, code, signal_date FROM strategy_signals").fetchall()
        for signal_id, code, signal_date in rows:
            next_return = returns_by_code[code]
            conn.execute(
                """
                INSERT OR REPLACE INTO strategy_outcomes
                (signal_id, code, next_trade_date, future_days, next_close_return,
                 signal_next_close_return, signal_exit_return, exit_return, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal_id,
                    code,
                    signal_date,
                    5,
                    next_return,
                    next_return,
                    next_return,
                    next_return,
                    "2026-01-04T15:00:00",
                ),
            )


class _FakeProvider:
    def get_history(self, code, days=180):
        import pandas as pd

        close = 10.2 if code == "000001" else 9.6
        return pd.DataFrame(
            {
                "trade_date": ["20260102", "20260103", "20260104", "20260105", "20260106", "20260107"],
                "open": [10.0] * 6,
                "high": [10.1] + [max(10.0, close)] * 5,
                "low": [9.9] + [min(10.0, close)] * 5,
                "price": [10.0] + [close] * 5,
            }
        )


if __name__ == "__main__":
    unittest.main()

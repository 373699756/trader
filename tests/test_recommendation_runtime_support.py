import unittest
from unittest.mock import patch

import pandas as pd

from stock_analyzer import config
from stock_analyzer import app_runtime_support as app_runtime
from stock_analyzer.app_runtime_support import finalize_deepseek_meta
from stock_analyzer import recommendation_runtime_support as support


class RecommendationRuntimeSupportTest(unittest.TestCase):
    def test_validation_review_waits_for_enough_new_real_days(self):
        class Store:
            def list_tuning_runs(self, strategy, limit=30):
                return [
                    {
                        "metrics": {"real_day_count": 7},
                        "deepseek": {"status": "ok"},
                    }
                ]

        with patch.object(config, "DEEPSEEK_VALIDATION_REVIEW_MIN_NEW_DAYS", 5), patch.object(
            app_runtime,
            "review_strategy_validation",
            side_effect=AssertionError("review should be deferred"),
        ):
            result = app_runtime.deepseek_validation_review(
                Store(),
                "tomorrow_picks",
                {"real_day_count": 10},
                20,
            )

        self.assertEqual(result["status"], "cadence_deferred")
        self.assertEqual(result["last_review_real_day_count"], 7)

    def test_validation_review_runs_after_five_new_real_days(self):
        class Store:
            def list_tuning_runs(self, strategy, limit=30):
                return [{"metrics": {"real_day_count": 7}, "deepseek": {"status": "ok"}}]

            def live_weight_samples(self, strategy, days=60):
                return []

        with patch.object(config, "DEEPSEEK_VALIDATION_REVIEW_MIN_NEW_DAYS", 5), patch.object(
            app_runtime,
            "review_strategy_validation",
            return_value={"enabled": True, "status": "ok"},
        ) as review:
            result = app_runtime.deepseek_validation_review(
                Store(),
                "tomorrow_picks",
                {"real_day_count": 12},
                20,
            )

        self.assertEqual(result["status"], "ok")
        review.assert_called_once()

    def test_finalize_deepseek_meta_recounts_tomorrow_tiers_after_filtering(self):
        meta = {
            "strategy_version": config.TOMORROW_STRATEGY_VERSION,
            "primary_watch_count": 2,
            "backup_watch_count": 1,
        }

        finalize_deepseek_meta(
            meta,
            [{"code": "600001", "tier": "backup_pool"}],
            {"status": "ok", "filtered": 2},
        )

        self.assertEqual(meta["display_count"], 1)
        self.assertEqual(meta["primary_watch_count"], 0)
        self.assertEqual(meta["backup_watch_count"], 1)

    def test_market_gate_risk_off_shrinks_rows_and_applies_tomorrow_threshold(self):
        rows = {
            "short_term": [{"code": str(index), "score": 80} for index in range(10)],
            "tomorrow_picks": [
                {"code": "a", "score": 80},
                {"code": "b", "score": 70},
                {"code": "c", "score": 60},
            ],
            "swing_picks": [{"code": str(index), "score": 75} for index in range(5)],
        }
        with patch.object(config, "TOMORROW_PRIMARY_MIN_SCORE", 68.0), patch.object(
            config, "DEEPSEEK_MARKET_GATE_RISK_OFF_SCORE_BONUS", 5.0
        ):
            gated, counts = support._apply_market_gate(rows, {"regime": "risk_off", "size_factor": 0.4})

        self.assertEqual(len(gated["short_term"]), 4)
        self.assertEqual([row["code"] for row in gated["tomorrow_picks"]], ["a"])
        self.assertEqual(len(gated["swing_picks"]), 2)
        self.assertEqual(counts["tomorrow_picks"], {"before": 3, "after": 1})

    def test_market_gate_context_prefers_full_market_breadth_over_candidate_pool(self):
        candidates = pd.DataFrame(
            [
                {"code": "600001", "pct_chg": 5.0, "turnover": 100},
                {"code": "600002", "pct_chg": 4.0, "turnover": 100},
            ]
        )
        context = support._market_gate_context(
            candidates,
            {
                "breadth_sample_count": 100,
                "up_count": 25,
                "down_count": 70,
                "limit_up_count": 1,
                "limit_down_count": 3,
                "avg_pct_chg": -0.8,
                "median_pct_chg": -1.0,
            },
        )

        self.assertEqual(context["breadth_source"], "full_market_snapshot")
        self.assertEqual(context["sample_count"], 100)
        self.assertEqual(context["up_ratio_pct"], 25.0)
        self.assertEqual(context["down_ratio_pct"], 70.0)
        self.assertEqual(context["limit_down_count"], 3)

    def test_build_recommendation_horizons_uses_one_batch_rerank(self):
        with patch.object(
            support,
            "score_today_picks",
            return_value=({"short_term": [{"code": "A", "score": 90}]}, {"strategy_version": "today_v1"}),
        ), patch.object(
            support,
            "score_tomorrow_picks",
            return_value=([{"code": "B", "score": 88}], {"strategy_version": "tomorrow_v1"}),
        ), patch.object(
            support,
            "score_swing_2_5d_picks",
            return_value=([{"code": "C", "score": 86}], {"strategy_version": "swing_v1"}),
        ), patch.object(
            support,
            "apply_deepseek_rerank_batch",
            return_value=(
                {
                    "short_term": [{"code": "A", "score": 91}],
                    "tomorrow_picks": [{"code": "B", "score": 89}],
                    "swing_picks": [{"code": "C", "score": 87}],
                },
                {
                    "short_term": {"status": "ok", "source": "deepseek_batch"},
                    "tomorrow_picks": {"status": "ok", "source": "deepseek_batch"},
                    "swing_picks": {"status": "ok", "source": "deepseek_batch"},
                },
            ),
        ) as batch, patch.object(
            support,
            "apply_deepseek_rerank",
            side_effect=AssertionError("single-strategy rerank should not be used"),
        ), patch.object(config, "ENABLE_DEEPSEEK_MARKET_GATE", False):
            rows, _, meta = support.build_recommendation_horizons(
                candidates=None,
                top_n=5,
                market="all",
                market_regime={},
                hot_ranks={},
                industry_strength={},
                sentiment_lookup={},
                cached_metrics_fn=lambda strategy, days: {"sample_count": 0},
                apply_deepseek=True,
            )

        batch.assert_called_once()
        self.assertEqual(rows["short_term"][0]["score"], 91)
        self.assertEqual(meta["short_term"]["source"], "deepseek_batch")
        self.assertEqual(meta["tomorrow_picks"]["source"], "deepseek_batch")
        self.assertEqual(meta["swing_picks"]["source"], "deepseek_batch")

    def test_build_recommendation_horizons_fails_safe_when_validation_metrics_are_unavailable(self):
        with patch.object(
            support,
            "score_today_picks",
            return_value=({"short_term": []}, {"strategy_version": "today_v1"}),
        ), patch.object(
            support,
            "score_tomorrow_picks",
            return_value=(
                [{"code": "T1", "score": 88, "tier": "primary_watch", "reasons": []}],
                {"strategy_version": config.TOMORROW_STRATEGY_VERSION},
            ),
        ), patch.object(
            support,
            "score_swing_2_5d_picks",
            return_value=([], {"strategy_version": "swing_v1"}),
        ), patch.object(config, "ENABLE_DEEPSEEK_MARKET_GATE", False):
            rows, _, _ = support.build_recommendation_horizons(
                candidates=None,
                top_n=5,
                market="all",
                market_regime={},
                hot_ranks={},
                industry_strength={},
                sentiment_lookup={},
                cached_metrics_fn=lambda strategy, days: (_ for _ in ()).throw(RuntimeError("db locked")),
                apply_deepseek=False,
            )

        self.assertEqual(rows["tomorrow_picks"][0]["tier"], "backup_pool")
        self.assertFalse(rows["tomorrow_picks"][0]["execution_allowed"])

    def test_prediction_strategy_rows_respects_short_term_override(self):
        with patch.object(
            support,
            "score_today_picks",
            return_value=({"short_term": [{"code": "RAW", "score": 70}]}, {"strategy_version": "today_v1"}),
        ), patch.object(
            support,
            "apply_deepseek_rerank",
            return_value=([{"code": "RAW", "score": 70}], {"status": "ok"}),
        ), patch.object(
            support,
            "scored_strategy_rows",
            side_effect=[
                ([{"code": "T1", "score": 80}], {"strategy_version": "tomorrow_v1"}, {"status": "ok"}),
                ([{"code": "S1", "score": 60}], {"strategy_version": "swing_v1"}, {"status": "ok"}),
            ],
        ):
            rows, metas = support.prediction_strategy_rows(
                candidates=None,
                top_n=5,
                market_regime={},
                hot_ranks={},
                industry_strength={},
                sentiment_lookup={},
                short_term_rows_override=[{"code": "SHOW", "score": 88}],
                short_term_meta_override={"missed_reason": "展示裁剪未入榜"},
            )

        self.assertEqual(rows["short_term"], [{"code": "SHOW", "score": 88}])
        self.assertEqual(metas["short_term"]["strategy_version"], "today_v1")
        self.assertEqual(metas["short_term"]["missed_reason"], "展示裁剪未入榜")
        self.assertEqual(metas["tomorrow_picks"]["strategy_version"], "tomorrow_v1")
        self.assertEqual(metas["swing_picks"]["strategy_version"], "swing_v1")

    def test_prediction_strategy_rows_applies_tomorrow_validation_gate(self):
        tomorrow_rows = [
            {
                "code": "T1",
                "tier": "primary_watch",
                "tier_label": "重点观察",
                "reasons": [],
            }
        ]
        with patch.object(
            support,
            "score_today_picks",
            return_value=({"short_term": []}, {"strategy_version": "today_v1"}),
        ), patch.object(
            support,
            "apply_deepseek_rerank",
            return_value=([], {"status": "empty"}),
        ), patch.object(
            support,
            "scored_strategy_rows",
            side_effect=[
                (tomorrow_rows, {"strategy_version": config.TOMORROW_STRATEGY_VERSION}, {"status": "ok"}),
                ([], {"strategy_version": "swing_v1"}, {"status": "empty"}),
            ],
        ):
            rows, metas = support.prediction_strategy_rows(
                candidates=None,
                top_n=5,
                market_regime={},
                hot_ranks={},
                industry_strength={},
                sentiment_lookup={},
                cached_metrics_fn=lambda strategy, days: {
                    "strategy_name": strategy,
                    "real_sample_count": 20,
                    "real_day_count": 60,
                    "real_avg_primary_return_net": -1.0,
                    "real_win_rate_primary_net": 20.0,
                },
            )

        self.assertEqual(rows["tomorrow_picks"][0]["tier"], "backup_pool")
        self.assertFalse(rows["tomorrow_picks"][0]["execution_allowed"])
        self.assertTrue(metas["tomorrow_picks"]["validation_gate"]["blocked"])

    def test_prediction_strategy_rows_fails_safe_when_validation_metrics_are_unavailable(self):
        tomorrow_rows = [
            {
                "code": "T1",
                "tier": "primary_watch",
                "tier_label": "重点观察",
                "reasons": [],
            }
        ]
        with patch.object(
            support,
            "score_today_picks",
            return_value=({"short_term": []}, {"strategy_version": "today_v1"}),
        ), patch.object(
            support,
            "apply_deepseek_rerank",
            return_value=([], {"status": "empty"}),
        ), patch.object(
            support,
            "scored_strategy_rows",
            side_effect=[
                (tomorrow_rows, {"strategy_version": config.TOMORROW_STRATEGY_VERSION}, {"status": "ok"}),
                ([], {"strategy_version": "swing_v1"}, {"status": "empty"}),
            ],
        ):
            rows, metas = support.prediction_strategy_rows(
                candidates=None,
                top_n=5,
                market_regime={},
                hot_ranks={},
                industry_strength={},
                sentiment_lookup={},
                cached_metrics_fn=lambda strategy, days: (_ for _ in ()).throw(RuntimeError("db locked")),
            )

        self.assertEqual(rows["tomorrow_picks"][0]["tier"], "backup_pool")
        self.assertFalse(rows["tomorrow_picks"][0]["execution_allowed"])
        self.assertEqual(metas["tomorrow_picks"]["validation_gate"]["state"], "unavailable")
        self.assertTrue(metas["tomorrow_picks"]["validation_gate"]["blocked"])

    def test_finalize_recommendation_payload_meta_adds_stability_and_blacklist_summary(self):
        meta = {}
        short_rows = [{"code": "000001", "score": 90}, {"code": "000002", "score": 80}]

        class DummyValidationStore:
            def metrics(self, strategy_name, days=20):
                return {"sample_count": 0}

            def live_weight_samples(self, strategy_name, days=60):
                return []

        rows, meta = support.finalize_recommendation_payload_meta(
            short_rows=short_rows,
            meta=meta,
            blacklist_payload={"status": "ok", "items": {"000001": {}}, "sources": ["risk.json"], "errors": []},
            hard_filter_report={"raw_count": 10, "passed_count": 2, "rejected_count": 8},
            market_regime={"label": "偏强"},
            deepseek_meta_by_strategy={"short_term": {"status": "ok"}},
            top_n=2,
            stability_update_fn=lambda horizon, rows: {
                "rows": rows,
                "new_entries": ["000001"],
                "dropped": ["000003"],
                "retained": ["000002"],
                "last_updated": "2026-07-09T10:00:00",
            },
            validation_store=DummyValidationStore(),
            cached_metrics_fn=lambda strategy, days: {"sample_count": 0},
        )

        self.assertEqual([row["code"] for row in rows], ["000001", "000002"])
        self.assertEqual(meta["top_n"], 2)
        self.assertEqual(meta["risk_blacklist"]["item_count"], 1)
        self.assertEqual(meta["hard_filter_report"]["passed_count"], 2)
        self.assertEqual(meta["market_regime"]["label"], "偏强")
        self.assertEqual(meta["stability"]["short_term"]["new_entries"], ["000001"])
        self.assertEqual(meta["display_theme_cap"], 3)

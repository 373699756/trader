import unittest
import tempfile
from unittest.mock import patch

import pandas as pd

from stock_analyzer import config
from stock_analyzer import app_runtime_support as app_runtime
from stock_analyzer.app_runtime_support import finalize_deepseek_meta
from stock_analyzer import recommendation_runtime_support as support


class RecommendationRuntimeSupportTest(unittest.TestCase):
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

    def test_build_recommendation_horizons_fails_safe_when_validation_metrics_are_unavailable(self):
        with patch.object(
            support,
            "score_today_picks",
            return_value=({"today_term": []}, {"strategy_version": "today_v1"}),
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

    def test_build_recommendation_horizons_attaches_long_term_watch_without_changing_primary_rows(self):
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
                }
            ]
        )
        with patch.object(
            support,
            "score_today_picks",
            return_value=({"today_term": [{"code": "002371", "score": 80}]}, {"strategy_version": "today_v1"}),
        ), patch.object(
            support,
            "score_tomorrow_picks",
            return_value=([], {"strategy_version": config.TOMORROW_STRATEGY_VERSION}),
        ), patch.object(
            support,
            "score_swing_2_5d_picks",
            return_value=([], {"strategy_version": "swing_v1"}),
        ), patch.object(config, "ENABLE_DEEPSEEK_MARKET_GATE", False):
            rows, _, _ = support.build_recommendation_horizons(
                candidates=candidates,
                top_n=5,
                market="all",
                market_regime={},
                hot_ranks={},
                industry_strength={},
                sentiment_lookup={},
                cached_metrics_fn=lambda strategy, days: {"sample_count": 0},
                apply_deepseek=False,
            )

        self.assertEqual(rows["today_term"][0]["code"], "002371")
        self.assertIn("long_term_watch", rows)
        self.assertEqual(rows["long_term_watch"][0]["code"], "002371")
        self.assertFalse(rows["long_term_watch"][0]["execution_allowed"])

    def test_scored_strategy_rows_promotes_expected_return_ranking_after_gate_passes(self):
        class Store:
            def live_weight_samples(self, strategy_name, days=180):
                return [
                    {
                        "signal_date": "2024-01-{:02d}".format((idx % 2) + 1),
                        "stored_score": 80 if idx % 2 else 70,
                        "primary_return_net": 1.0,
                        "raw": {"score": 80 if idx % 2 else 70},
                    }
                    for idx in range(20)
                ]

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(config, "ENABLE_EXPECTED_RETURN_RANKING", True), patch.object(
                config, "EXPECTED_RETURN_MIN_REAL_DAYS", 2
            ), patch.object(config, "EXPECTED_RETURN_ARTIFACT_DIR", tmpdir), patch.object(
                support,
                "evaluate_expected_return_ranker",
                return_value={
                    "ok": True,
                    "status": "oos_passed",
                    "can_promote": True,
                    "baseline_oos_objective": 0.1,
                    "rank_score_oos_objective": 0.3,
                    "positive_folds": 3,
                    "fold_count": 3,
                    "margin": 0.05,
                    "fdr": {"passed": True},
                    "ci": {"passed": True, "low": 0.02, "high": 0.2},
                    "folds": [{"fold": 1}],
                },
            ), patch.object(
                support,
                "score_tomorrow_picks",
                return_value=([{"code": "T1", "score": 80}], {"strategy_version": "tomorrow_v1"}),
            ) as scorer:
                rows, meta, _ = support.scored_strategy_rows(
                    "tomorrow_picks",
                    candidates=None,
                    top_n=5,
                    market="all",
                    market_regime={},
                    apply_deepseek=False,
                    validation_store=Store(),
                )

        kwargs = scorer.call_args.kwargs
        self.assertTrue(kwargs["use_expected_return_ranking"])
        self.assertEqual(len(kwargs["expected_return_samples"]), 20)
        self.assertEqual(meta["expected_return_ranking"]["status"], "active")
        self.assertNotIn("folds", meta["expected_return_ranking"]["gate"])
        self.assertEqual(rows[0]["code"], "T1")

    def test_expected_return_context_builds_execution_policy_baseline_for_primary_strategies(self):
        class Store:
            def live_weight_samples(self, strategy_name, days=180):
                return []

        with patch.object(config, "ENABLE_EXPECTED_RETURN_RANKING", True):
            contexts = [
                support.expected_return_ranking_context(
                    strategy_name,
                    validation_store=Store(),
                    top_k=5,
                )
                for strategy_name in ("tomorrow_picks", "swing_picks")
            ]

        for context in contexts:
            self.assertFalse(context["use_ranking"])
            self.assertEqual(context["meta"]["status"], "insufficient_real_days")
            self.assertIn("validation_baseline_id", context["meta"])
            self.assertIn("policy_", context["meta"]["validation_baseline_id"])

    def test_scored_strategy_rows_keeps_expected_return_shadow_when_gate_fails(self):
        class Store:
            def live_weight_samples(self, strategy_name, days=180):
                return [
                    {
                        "signal_date": "2024-01-{:02d}".format((idx % 2) + 1),
                        "stored_score": 80 if idx % 2 else 70,
                        "primary_return_net": -1.0 if idx % 2 else 0.5,
                        "raw": {"score": 80 if idx % 2 else 70},
                    }
                    for idx in range(20)
                ]

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(config, "ENABLE_EXPECTED_RETURN_RANKING", True), patch.object(
                config, "EXPECTED_RETURN_MIN_REAL_DAYS", 2
            ), patch.object(config, "EXPECTED_RETURN_ARTIFACT_DIR", tmpdir), patch.object(
                support,
                "evaluate_expected_return_ranker",
                return_value={
                    "ok": True,
                    "status": "shadow_only",
                    "can_promote": False,
                    "baseline_oos_objective": 0.2,
                    "rank_score_oos_objective": 0.1,
                    "positive_folds": 1,
                    "fold_count": 3,
                    "margin": 0.05,
                    "fdr": {"passed": True},
                    "ci": {"passed": False, "low": -0.1, "high": 0.05},
                },
            ), patch.object(
                support,
                "score_swing_2_5d_picks",
                return_value=([{"code": "S1", "score": 70}], {"strategy_version": "swing_v1"}),
            ) as scorer:
                rows, meta, _ = support.scored_strategy_rows(
                    "swing_picks",
                    candidates=None,
                    top_n=5,
                    market="all",
                    market_regime={},
                    apply_deepseek=False,
                    validation_store=Store(),
                )

        kwargs = scorer.call_args.kwargs
        self.assertFalse(kwargs["use_expected_return_ranking"])
        self.assertEqual(len(kwargs["expected_return_samples"]), 20)
        self.assertEqual(meta["expected_return_ranking"]["status"], "oos_blocked")
        self.assertEqual(rows[0]["code"], "S1")

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
            deepseek_meta_by_strategy={"today_term": {"status": "ok"}},
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
        self.assertEqual(meta["stability"]["today_term"]["new_entries"], ["000001"])
        self.assertEqual(meta["display_theme_cap"], 3)

    def test_finalize_recommendation_payload_meta_preserves_operational_deepseek_statuses(self):
        class DummyValidationStore:
            def metrics(self, strategy_name, days=20):
                return {"sample_count": 0}

            def live_weight_samples(self, strategy_name, days=60):
                return []

        def finalize(status, **extra):
            _, result = support.finalize_recommendation_payload_meta(
                short_rows=[{"code": "000001", "score": 90}],
                meta={},
                blacklist_payload={"status": "ok", "items": {}, "sources": [], "errors": []},
                hard_filter_report={},
                market_regime={},
                deepseek_meta_by_strategy={"today_term": {"status": status, **extra}},
                top_n=1,
                stability_update_fn=lambda horizon, rows: {
                    "rows": rows,
                    "new_entries": [],
                    "dropped": [],
                    "retained": [],
                    "last_updated": "",
                },
                validation_store=DummyValidationStore(),
                cached_metrics_fn=lambda strategy, days: {"sample_count": 0},
            )
            return result["deepseek"]["status"]

        for status in ("daily_call_limit", "deadline_skipped", "disabled", "abstain"):
            self.assertEqual(finalize(status), status)
        self.assertEqual(
            finalize("error", error_type="api_error", error_message="request failed"),
            "error",
        )

    def test_finalize_recommendation_payload_meta_reports_cache_only_and_production_states(self):
        class DummyValidationStore:
            def metrics(self, strategy_name, days=20):
                return {"sample_count": 0}

        def finalize(items):
            _, result = support.finalize_recommendation_payload_meta(
                short_rows=[{"code": "000001", "score": 90}],
                meta={},
                blacklist_payload={"status": "ok", "items": {}, "sources": [], "errors": []},
                hard_filter_report={},
                market_regime={},
                deepseek_meta_by_strategy=items,
                top_n=1,
                stability_update_fn=lambda horizon, rows: {
                    "rows": rows,
                    "new_entries": [],
                    "dropped": [],
                    "retained": [],
                    "last_updated": "",
                },
                validation_store=DummyValidationStore(),
                cached_metrics_fn=lambda strategy, days: {"sample_count": 0},
            )
            return result["deepseek"]

        cache_only = finalize(
            {
                "today_term": {
                    "status": "cache_hit",
                    "production_applied": True,
                }
            }
        )
        self.assertEqual(cache_only["status"], "cache_hit")
        self.assertTrue(cache_only["production_applied"])
        mixed_production = finalize(
            {
                "today_term": {"status": "cache_hit", "production_applied": True},
                "tomorrow_picks": {"status": "precomputed", "production_applied": True},
            }
        )
        self.assertEqual(mixed_production["status"], "precomputed")

    def test_apply_deepseek_after_gate_uses_public_disabled_status_when_skipped(self):
        service = support.RecommendationService(cached_metrics_fn=lambda strategy, days: {})
        meta = service._apply_deepseek_after_gate(
            {"today_term": [{"code": "000001", "score": 80}]},
            "all",
            apply_deepseek=False,
        )

        self.assertEqual(meta["today_term"]["status"], "disabled")

    def test_finalize_payload_tightens_display_theme_cap_in_risk_off(self):
        short_rows = [
            {"code": "000001", "theme": "AI", "score": 90},
            {"code": "000002", "theme": "AI", "score": 89},
            {"code": "000003", "theme": "AI", "score": 88},
            {"code": "000004", "theme": "医药", "score": 87},
        ]

        class DummyValidationStore:
            def metrics(self, strategy_name, days=20):
                return {"sample_count": 0}

            def live_weight_samples(self, strategy_name, days=60):
                return []

        with patch.object(config, "ENABLE_REGIME_THEME_CAP", True), patch.object(
            config, "RECOMMENDATION_THEME_CAP_RISK_OFF_DELTA", 1
        ):
            rows, meta = support.finalize_recommendation_payload_meta(
                short_rows,
                meta={},
                blacklist_payload={"status": "ok", "items": {}, "sources": [], "errors": []},
                hard_filter_report={},
                market_regime={"level": "risk_off", "score": 35},
                deepseek_meta_by_strategy={},
                top_n=4,
                stability_update_fn=lambda horizon, items: {
                    "rows": items,
                    "new_entries": [],
                    "dropped": [],
                    "retained": [],
                    "last_updated": "",
                },
                validation_store=DummyValidationStore(),
                cached_metrics_fn=lambda strategy, days: {"sample_count": 0},
            )

        self.assertEqual(meta["base_display_theme_cap"], 3)
        self.assertEqual(meta["display_theme_cap"], 2)
        self.assertEqual([row["code"] for row in rows], ["000001", "000004", "000002"])

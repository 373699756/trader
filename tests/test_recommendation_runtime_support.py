import unittest
from unittest.mock import patch

from stock_analyzer import recommendation_runtime_support as support


class RecommendationRuntimeSupportTest(unittest.TestCase):
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


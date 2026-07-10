import unittest
from unittest.mock import patch

from stock_analyzer import app_response_support as support


class AppResponseSupportTest(unittest.TestCase):
    def test_saved_tomorrow_fallback_payload_non_detailed_uses_snapshot_shape(self):
        rows = [{"code": "000001", "name": "样本A"}]
        with patch.object(support, "attach_validation_summary") as attach_summary:
            payload = support.saved_tomorrow_fallback_payload(
                saved_rows=rows,
                top_n=5,
                market="all",
                detailed=False,
                validation_store=object(),
                cached_metrics_fn=lambda strategy, days: {"sample_count": 0},
                load_risk_blacklist_fn=lambda: {"status": "ok", "items": {"000001": {}}, "sources": [], "errors": []},
                analysis_window_fn=lambda: "15:00",
                provider_health_fn=lambda: {"provider": "ok"},
                research_disclaimer_fn=lambda: "仅供研究",
            )

        attach_summary.assert_called_once()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"], rows)
        self.assertEqual(payload["meta"]["fallback"], "saved_snapshot")
        self.assertEqual(payload["meta"]["risk_blacklist"]["item_count"], 1)
        self.assertEqual(payload["health"], {"provider": "ok"})
        self.assertEqual(payload["disclaimer"], "仅供研究")

    def test_saved_tomorrow_fallback_payload_detailed_adds_analysis_window(self):
        rows = [
            {
                "code": "000001",
                "name": "样本A",
                "tier": "backup_pool",
                "tier_label": "备选观察",
                "strategy_version": "tomorrow_picks_v4",
            }
        ]
        with patch.object(support, "attach_validation_summary"), patch.object(
            support, "apply_tomorrow_validation_gate"
        ) as apply_gate:
            payload = support.saved_tomorrow_fallback_payload(
                saved_rows=rows,
                top_n=3,
                market="all",
                detailed=True,
                validation_store=object(),
                cached_metrics_fn=lambda strategy, days: {"sample_count": 2},
                load_risk_blacklist_fn=lambda: {"status": "ok", "items": {}, "sources": [], "errors": []},
                analysis_window_fn=lambda: "15:30",
                provider_health_fn=lambda: {"provider": "ok"},
                research_disclaimer_fn=lambda: "仅供研究",
            )

        apply_gate.assert_called_once()
        self.assertEqual(payload["meta"]["analysis_window"], "15:30")
        self.assertEqual(payload["meta"]["strategy_label"], "明日优先")
        self.assertEqual(payload["meta"]["display_count"], 1)
        self.assertEqual(payload["meta"]["strategy_version"], "tomorrow_picks_v4")
        self.assertEqual(payload["meta"]["primary_watch_count"], 0)
        self.assertEqual(payload["meta"]["backup_watch_count"], 1)

    def test_saved_tomorrow_fallback_recounts_tiers_after_validation_gate(self):
        rows = [
            {
                "code": "000001",
                "name": "样本A",
                "tier": "primary_watch",
                "tier_label": "重点观察",
                "strategy_version": support.config.TOMORROW_STRATEGY_VERSION,
            }
        ]

        def demote(saved_rows, meta, metrics):
            saved_rows[0]["tier"] = "backup_pool"
            saved_rows[0]["tier_label"] = "备选观察"
            return {"blocked": True}

        with patch.object(support, "attach_validation_summary"), patch.object(
            support, "apply_tomorrow_validation_gate", side_effect=demote
        ):
            payload = support.saved_tomorrow_fallback_payload(
                saved_rows=rows,
                top_n=3,
                market="all",
                detailed=True,
                validation_store=object(),
                cached_metrics_fn=lambda strategy, days: {"sample_count": 20},
                load_risk_blacklist_fn=lambda: {"status": "ok", "items": {}, "sources": [], "errors": []},
                analysis_window_fn=lambda: "15:00",
                provider_health_fn=lambda: {"provider": "ok"},
                research_disclaimer_fn=lambda: "仅供研究",
            )

        self.assertEqual(payload["meta"]["primary_watch_count"], 0)
        self.assertEqual(payload["meta"]["backup_watch_count"], 1)

    def test_saved_tomorrow_non_detailed_also_applies_validation_gate(self):
        rows = [
            {
                "code": "000001",
                "tier": "primary_watch",
                "tier_label": "重点观察",
                "strategy_version": support.config.TOMORROW_STRATEGY_VERSION,
            }
        ]

        def demote(saved_rows, meta, metrics):
            saved_rows[0]["tier"] = "backup_pool"
            saved_rows[0]["tier_label"] = "备选观察"
            meta["validation_gate"] = {"blocked": True}
            return meta["validation_gate"]

        with patch.object(support, "attach_validation_summary"), patch.object(
            support, "apply_tomorrow_validation_gate", side_effect=demote
        ) as apply_gate:
            payload = support.saved_tomorrow_fallback_payload(
                saved_rows=rows,
                top_n=3,
                market="all",
                detailed=False,
                validation_store=object(),
                cached_metrics_fn=lambda strategy, days: {"sample_count": 20},
                load_risk_blacklist_fn=lambda: {"status": "ok", "items": {}, "sources": [], "errors": []},
                analysis_window_fn=lambda: "15:00",
                provider_health_fn=lambda: {"provider": "ok"},
                research_disclaimer_fn=lambda: "仅供研究",
            )

        apply_gate.assert_called_once()
        self.assertEqual(payload["meta"]["primary_watch_count"], 0)
        self.assertEqual(payload["meta"]["backup_watch_count"], 1)
        self.assertTrue(payload["meta"]["validation_gate"]["blocked"])

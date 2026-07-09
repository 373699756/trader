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
        rows = [{"code": "000001", "name": "样本A"}]
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
        self.assertEqual(payload["meta"]["strategy_label"], "明天推荐")
        self.assertEqual(payload["meta"]["display_count"], 1)

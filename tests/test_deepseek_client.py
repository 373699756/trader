import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

from stock_analyzer import deepseek_client


class DeepSeekClientTest(unittest.TestCase):
    def _rows(self):
        return [
            {
                "code": "000001",
                "name": "样本A",
                "score": 80,
                "pct_chg": 3,
                "volume_ratio": 2,
                "turnover_rate": 5,
                "amplitude": 4,
                "execution_score": 80,
                "trend_score": 70,
                "momentum_score": 72,
            },
            {
                "code": "000002",
                "name": "样本B",
                "score": 90,
                "pct_chg": 8,
                "volume_ratio": 6,
                "turnover_rate": 20,
                "amplitude": 12,
                "execution_score": 40,
            },
            {"code": "000003", "name": "样本C", "score": 60, "pct_chg": 1},
        ]

    def test_rerank_returns_missing_key_without_calling_api(self):
        env = {
            "DEEPSEEK_ENABLED": "1",
            "DEEPSEEK_API_KEY": "",
            "DEEPSEEK_CACHE_ENABLED": "0",
        }
        with patch.dict(os.environ, env, clear=False), patch.object(deepseek_client, "_load_dotenv_if_needed"), patch(
            "stock_analyzer.deepseek_client.requests.post"
        ) as post:
            rows, meta = deepseek_client.rerank_candidates(self._rows(), "swing_picks")
        self.assertEqual(rows[0]["code"], "000001")
        self.assertEqual(meta["status"], "missing_api_key")
        post.assert_not_called()

    def test_rerank_rejects_unknown_strategy_without_calling_api(self):
        env = {
            "DEEPSEEK_ENABLED": "1",
            "DEEPSEEK_API_KEY": "test-key",
            "DEEPSEEK_CACHE_ENABLED": "0",
        }
        with patch.dict(os.environ, env, clear=False), patch.object(deepseek_client, "_load_dotenv_if_needed"), patch(
            "stock_analyzer.deepseek_client.requests.post"
        ) as post:
            _, meta = deepseek_client.rerank_candidates(self._rows(), "unknown_strategy")
        self.assertEqual(meta["status"], "strategy_not_supported")
        post.assert_not_called()

    def test_safe_parse_json_tolerates_wrapped_model_output(self):
        parsed = deepseek_client._safe_parse_json(
            "下面是JSON:\n```json\n{\"decision\":\"watch\",\"rule_candidates\":[{\"field\":\"factor_snapshot.ret_20d\",}],}\n```\n请参考"
        )
        self.assertEqual(parsed["decision"], "watch")
        self.assertEqual(parsed["rule_candidates"][0]["field"], "factor_snapshot.ret_20d")

    def test_rerank_merges_structured_strategy_fields(self):
        response = MagicMock()
        response.status_code = 200
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "results": [
                                    {
                                        "code": "000001",
                                        "llm_score": 88,
                                        "horizon_up_score": 92,
                                        "action": "priority",
                                        "veto": False,
                                        "penalty": 1,
                                        "reason": "趋势承接较好",
                                        "risk_flags": ["无明显透支"],
                                        "catalyst_score": 66,
                                        "theme_truth_score": 72,
                                        "event_risk_score": 18,
                                    },
                                    {
                                        "code": "000002",
                                        "llm_score": 40,
                                        "horizon_up_score": 35,
                                        "action": "avoid",
                                        "veto": True,
                                        "penalty": 25,
                                        "reason": "过热",
                                        "risk_flags": ["涨幅透支"],
                                    },
                                ]
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ],
            "usage": {"total_tokens": 123},
        }
        env = {
            "DEEPSEEK_ENABLED": "1",
            "DEEPSEEK_API_KEY": "test-key",
            "DEEPSEEK_CACHE_ENABLED": "0",
            "DEEPSEEK_RETRY_COUNT": "0",
            "DEEPSEEK_VALIDATION_TIMEOUT_SECONDS": "6",
            "DEEPSEEK_VALIDATION_RETRY_COUNT": "0",
        }
        with patch.dict(os.environ, env, clear=False), patch.object(deepseek_client, "_load_dotenv_if_needed"), patch(
            "stock_analyzer.deepseek_client.requests.post", return_value=response
        ):
            rows, meta = deepseek_client.rerank_candidates(self._rows(), "tech_potential")
        self.assertEqual(meta["status"], "ok")
        top = rows[0]
        self.assertEqual(top["code"], "000001")
        self.assertEqual(top["deepseek_horizon_score"], 94.8)
        self.assertEqual(top["deepseek_catalyst_score"], 66)
        self.assertEqual(top["deepseek_theme_truth_score"], 72)
        self.assertEqual(top["deepseek_event_risk_score"], 18)

    def test_validation_review_supports_swing_strategy_rules(self):
        response = MagicMock()
        response.status_code = 200
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "decision": "watch",
                                "summary": "高换手样本表现偏弱",
                                "avoid_conditions": ["高换手叠加高振幅"],
                                "suggested_filters": ["剔除明显过热样本"],
                                "suggested_penalties": [{"condition": "turnover_rate>15", "penalty": 8}],
                                "rule_candidates": [
                                    {
                                        "field": "turnover_rate",
                                        "operator": ">",
                                        "threshold": 15,
                                        "penalty": 8,
                                        "reason": "换手过热",
                                    }
                                ],
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ],
            "usage": {"total_tokens": 88},
        }
        env = {
            "DEEPSEEK_ENABLED": "1",
            "DEEPSEEK_API_KEY": "test-key",
            "DEEPSEEK_CACHE_ENABLED": "0",
            "DEEPSEEK_RETRY_COUNT": "0",
            "DEEPSEEK_VALIDATION_TIMEOUT_SECONDS": "6",
            "DEEPSEEK_VALIDATION_RETRY_COUNT": "0",
        }
        samples = [
            {
                "signal_date": "2026-01-02",
                "code": "000001",
                "name": "样本A",
                "rank": 1,
                "stored_score": 80,
                "primary_return_net": -3.2,
                "max_drawdown": -5.0,
                "raw": {"turnover_rate": 18, "volume_ratio": 3, "reasons": ["波段延续"]},
                "factor_snapshot": {"ret_20d": 12.34567, "ma20_gap": 4.2, "vol_ma5_ratio": 1.8, "ignored": 999},
            }
        ]
        metrics = {"sample_count": 10, "win_rate_primary_net": 45.0, "avg_primary_return_net": -0.5}
        with patch.dict(os.environ, env, clear=False), patch.object(deepseek_client, "_load_dotenv_if_needed"), patch(
            "stock_analyzer.deepseek_client.requests.post", return_value=response
        ) as post:
            review = deepseek_client.review_strategy_validation("swing_picks", metrics, samples, days=20)
        self.assertEqual(review["status"], "ok")
        self.assertEqual(review["rule_candidates"][0]["field"], "turnover_rate")
        request_payload = post.call_args.kwargs["json"]
        self.assertEqual(post.call_args.kwargs["timeout"], 6.0)
        self.assertIn("5-10日", request_payload["messages"][1]["content"])
        self.assertIn('"factor_snapshot"', request_payload["messages"][1]["content"])
        self.assertIn('"ret_20d": 12.3457', request_payload["messages"][1]["content"])
        self.assertNotIn('"ignored"', request_payload["messages"][1]["content"])
        self.assertGreaterEqual(request_payload["max_tokens"], 700)

    def test_validation_review_reports_timeout_status(self):
        env = {
            "DEEPSEEK_ENABLED": "1",
            "DEEPSEEK_API_KEY": "test-key",
            "DEEPSEEK_CACHE_ENABLED": "0",
            "DEEPSEEK_VALIDATION_RETRY_COUNT": "0",
        }
        with patch.dict(os.environ, env, clear=False), patch.object(deepseek_client, "_load_dotenv_if_needed"), patch(
            "stock_analyzer.deepseek_client.requests.post", side_effect=Exception("Read timed out.")
        ):
            review = deepseek_client.review_strategy_validation("tomorrow_picks", {"sample_count": 1}, [], days=1)
        self.assertEqual(review["status"], "timeout")

    def test_strategy_validation_endpoint_skips_deepseek_when_runtime_disabled(self):
        from stock_analyzer import config
        from stock_analyzer.app import create_app

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "validation.sqlite3")
            with patch.object(config, "VALIDATION_DB_PATH", db_path), patch.object(
                config, "ENABLE_DEEPSEEK_RUNTIME", False
            ), patch("stock_analyzer.app.review_strategy_validation") as review:
                app = create_app()
                app.config["TESTING"] = True
                response = app.test_client().get("/api/strategy-validation?strategy=tomorrow_picks&days=1")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["deepseek_review"]["status"], "runtime_disabled")
        review.assert_not_called()

    def test_hidden_strategy_route_skips_deepseek_rerank(self):
        from stock_analyzer import config
        from stock_analyzer.app import create_app

        provider = MagicMock()
        provider.get_realtime_quotes.return_value = pd.DataFrame([{"code": "000001", "name": "样本A"}])
        provider.health.return_value = {}
        candidates = pd.DataFrame([{"code": "000001", "name": "样本A", "pct_chg": 3, "turnover": 80000000}])
        rows = [{"code": "000001", "name": "样本A", "score": 80}]
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "validation.sqlite3")
            with patch.object(config, "VALIDATION_DB_PATH", db_path), patch.object(
                config, "ENABLE_DEEPSEEK_RUNTIME", True
            ), patch.object(
                config, "DEEPSEEK_RERANK_DISABLED_STRATEGIES", "tech_potential,chokepoint_picks"
            ), patch(
                "stock_analyzer.app.MarketDataProvider", return_value=provider
            ), patch(
                "stock_analyzer.app.prepare_candidates", return_value=candidates
            ), patch(
                "stock_analyzer.app.load_event_risk", return_value={}
            ), patch(
                "stock_analyzer.app.attach_event_risk", side_effect=lambda frame, payload: frame
            ), patch(
                "stock_analyzer.app.load_risk_blacklist", return_value={}
            ), patch(
                "stock_analyzer.app.attach_risk_blacklist", side_effect=lambda frame, payload: frame
            ), patch(
                "stock_analyzer.app.load_fundamentals", return_value={}
            ), patch(
                "stock_analyzer.app.attach_fundamental_factors", side_effect=lambda frame, payload: frame
            ), patch(
                "stock_analyzer.app.build_market_regime", return_value={}
            ), patch(
                "stock_analyzer.app.score_tech_potential_candidates", return_value=(rows, {"strategy_version": "tech_test"})
            ), patch(
                "stock_analyzer.app.rerank_candidates"
            ) as rerank:
                app = create_app()
                app.config["TESTING"] = True
                response = app.test_client().get("/api/tech-potential?top_n=10")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["meta"]["deepseek"]["status"], "strategy_rerank_disabled")
        rerank.assert_not_called()


if __name__ == "__main__":
    unittest.main()

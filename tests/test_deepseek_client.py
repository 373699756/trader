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

    def test_coerce_model_supports_only_v4_models(self):
        self.assertEqual(
            deepseek_client._coerce_model("deepseek-v4-flash", "deepseek-v4-flash"),
            "deepseek-v4-flash",
        )
        self.assertEqual(
            deepseek_client._coerce_model("deepseek-v4-pro", "deepseek-v4-pro"),
            "deepseek-v4-pro",
        )
        self.assertEqual(
            deepseek_client._coerce_model("unsupported", "deepseek-v4-flash"),
            "deepseek-v4-flash",
        )

    def test_coerce_base_url_supports_root_path(self):
        self.assertEqual(deepseek_client._coerce_base_url("https://api.deepseek.com"), "https://api.deepseek.com")

    def test_coerce_env_config_accepts_canonical_strategy_names(self):
        env = {
            "DEEPSEEK_ENABLED": "1",
            "DEEPSEEK_API_KEY": "test-key",
            "DEEPSEEK_STRATEGIES": "today_picks,swing_2_5d_picks",
            "DEEPSEEK_PRO_STRATEGIES": "swing_2_5d_picks",
        }
        with patch.dict(os.environ, env, clear=False), patch.object(deepseek_client, "_load_dotenv_if_needed"):
            config = deepseek_client._coerce_env_config()
        self.assertEqual(config["strategies"], ["short_term", "swing_picks"])
        self.assertEqual(config["pro_strategies"], ["swing_picks"])

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
                                        "event_type": "业绩",
                                        "sentiment": 1,
                                        "catalyst_strength": 84,
                                        "time_sensitivity": "明天",
                                        "already_priced_in": False,
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
            "DEEPSEEK_STRATEGIES": "swing_picks",
            "DEEPSEEK_VALIDATION_TIMEOUT_SECONDS": "6",
            "DEEPSEEK_VALIDATION_RETRY_COUNT": "0",
        }
        with patch.dict(os.environ, env, clear=False), patch.object(deepseek_client, "_load_dotenv_if_needed"), patch(
            "stock_analyzer.deepseek_client.requests.post", return_value=response
        ):
            rows, meta = deepseek_client.rerank_candidates(self._rows(), "swing_picks")
        self.assertEqual(meta["status"], "ok")
        top = rows[0]
        self.assertEqual(top["code"], "000001")
        self.assertEqual(top["deepseek_horizon_score"], 96.97)
        self.assertEqual(top["deepseek_event_score"], 76.06)
        self.assertEqual(top["deepseek_event_bonus"], 2.17)
        self.assertEqual(top["deepseek_event_penalty"], 0.0)
        self.assertEqual(top["deepseek_catalyst_score"], 66)
        self.assertEqual(top["deepseek_theme_truth_score"], 72)
        self.assertEqual(top["deepseek_event_risk_score"], 18)
        self.assertEqual(top["deepseek_event_type"], "业绩")
        self.assertEqual(top["deepseek_sentiment"], 1)
        self.assertEqual(top["deepseek_catalyst_strength"], 84)
        self.assertEqual(top["deepseek_time_sensitivity"], "明天")
        self.assertFalse(top["deepseek_already_priced_in"])
        self.assertNotIn("000002", [row["code"] for row in rows])
        self.assertEqual(meta["filtered"], 1)

    def test_rerank_filters_high_penalty_without_veto(self):
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
                                        "llm_score": 80,
                                        "horizon_up_score": 80,
                                        "action": "watch",
                                        "veto": False,
                                        "penalty": 30,
                                        "reason": "风险过高但未否决",
                                        "risk_flags": ["事件风险高"],
                                    }
                                ]
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ],
            "usage": {"total_tokens": 12},
        }
        env = {
            "DEEPSEEK_ENABLED": "1",
            "DEEPSEEK_API_KEY": "test-key",
            "DEEPSEEK_CACHE_ENABLED": "0",
            "DEEPSEEK_RETRY_COUNT": "0",
            "DEEPSEEK_STRATEGIES": "swing_picks",
        }
        rows = self._rows() + [
            {"code": "000004", "name": "样本D", "score": 55, "pct_chg": 0.5},
            {"code": "000005", "name": "样本E", "score": 54, "pct_chg": 0.2},
        ]
        with patch.dict(os.environ, env, clear=False), patch.object(deepseek_client, "_load_dotenv_if_needed"), patch(
            "stock_analyzer.deepseek_client.requests.post", return_value=response
        ):
            output_rows, meta = deepseek_client.rerank_candidates(rows, "swing_picks")
        self.assertNotIn("000001", [row["code"] for row in output_rows])
        self.assertIn("000001", meta["filtered_codes"])
        self.assertGreaterEqual(meta["filter_reasons"].get("deepseek_penalty_high", 0), 1)

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
            "DEEPSEEK_STRATEGIES": "swing_picks",
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
        self.assertIn("2-5日", request_payload["messages"][1]["content"])
        self.assertIn('"factor_snapshot"', request_payload["messages"][1]["content"])
        self.assertIn('"ret_20d": 12.3457', request_payload["messages"][1]["content"])
        self.assertNotIn('"ignored"', request_payload["messages"][1]["content"])
        self.assertGreaterEqual(request_payload["max_tokens"], 700)

    def test_rerank_request_uses_official_chat_endpoint(self):
        response = MagicMock()
        response.status_code = 200
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "choices": [{"message": {"content": json.dumps({"results": []})}}],
            "usage": {"total_tokens": 1},
        }
        env = {
            "DEEPSEEK_ENABLED": "1",
            "DEEPSEEK_API_KEY": "test-key",
            "DEEPSEEK_CACHE_ENABLED": "0",
            "DEEPSEEK_RETRY_COUNT": "0",
            "DEEPSEEK_STRATEGIES": "swing_2_5d_picks",
        }
        with patch.dict(os.environ, env, clear=False), patch.object(deepseek_client, "_load_dotenv_if_needed"), patch(
            "stock_analyzer.deepseek_client.requests.post", return_value=response
        ) as post:
            _, meta = deepseek_client.rerank_candidates(self._rows(), "swing_2_5d_picks")
        called_url = post.call_args.args[0] if post.call_args.args else post.call_args.kwargs.get("url")
        self.assertEqual(called_url, "https://api.deepseek.com/chat/completions")
        self.assertEqual(meta["strategy"], "swing_picks")

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

    def test_hidden_strategy_route_is_removed(self):
        from stock_analyzer import config
        from stock_analyzer.app import create_app

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "validation.sqlite3")
            with patch.object(config, "VALIDATION_DB_PATH", db_path), patch.object(config, "VALIDATION_AUTO_UPDATE_ENABLED", False):
                app = create_app()
                app.config["TESTING"] = True
                response = app.test_client().get("/api/tech-potential?top_n=10")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()

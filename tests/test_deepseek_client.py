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

    def test_intraday_provisional_rows_cannot_be_promoted_by_rerank(self):
        rows = [
            {
                **self._rows()[0],
                "observation_mode": "intraday_provisional",
                "trade_action": {"action": "watch_only", "position_size": 0.0},
            }
        ]
        llm_records = [
            {
                "code": "000001",
                "llm_score": 95,
                "tomorrow_up_score": 95,
                "action": "priority",
                "penalty": 0,
                "veto": False,
                "reason": "模型看多",
            }
        ]

        merged, _ = deepseek_client._merge_ranking_rows(rows, llm_records, 0.3, "tomorrow_picks")

        self.assertEqual(merged[0]["deepseek_action"], "watch")
        self.assertIn("盘中候选仅观察", merged[0]["deepseek_reason"])

    def test_backup_rows_cannot_be_promoted_by_rerank(self):
        rows = [{**self._rows()[0], "tier": "backup_pool", "tier_label": "备选观察"}]
        llm_records = [
            {
                "code": "000001",
                "llm_score": 95,
                "tomorrow_up_score": 95,
                "action": "priority",
                "penalty": 0,
                "veto": False,
                "reason": "模型看多",
            }
        ]

        merged, _ = deepseek_client._merge_ranking_rows(rows, llm_records, 0.3, "tomorrow_picks")

        self.assertEqual(merged[0]["deepseek_action"], "watch")
        self.assertIn("备选候选仅观察", merged[0]["deepseek_reason"])

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

    def test_rerank_skips_non_executable_candidates_before_api(self):
        env = {
            "DEEPSEEK_ENABLED": "1",
            "DEEPSEEK_API_KEY": "test-key",
            "DEEPSEEK_CACHE_ENABLED": "0",
            "DEEPSEEK_RETRY_COUNT": "0",
            "DEEPSEEK_STRATEGIES": "swing_picks",
        }
        rows = [
            {**self._rows()[0], "tier": "backup_pool", "execution_allowed": False},
            {**self._rows()[1], "observation_mode": "intraday_provisional"},
            {
                **self._rows()[2],
                "trade_action": {"action": "watch_only", "position_size": 0.0},
            },
        ]
        with patch.dict(os.environ, env, clear=False), patch.object(deepseek_client, "_load_dotenv_if_needed"), patch(
            "stock_analyzer.deepseek_client.requests.post"
        ) as post:
            output_rows, meta = deepseek_client.rerank_candidates(rows, "swing_picks")

        self.assertEqual(output_rows, rows)
        self.assertEqual(meta["status"], "no_executable_review_candidates")
        self.assertEqual(meta["review_policy"]["dropped_non_executable"], 3)
        post.assert_not_called()

    def test_pro_rerank_only_sends_boundary_candidates(self):
        response = MagicMock()
        response.status_code = 200
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {"results": [{"code": "000002", "llm_score": 92, "horizon_up_score": 88}]},
                            ensure_ascii=False,
                        )
                    }
                }
            ],
            "usage": {"total_tokens": 20},
        }
        env = {
            "DEEPSEEK_ENABLED": "1",
            "DEEPSEEK_API_KEY": "test-key",
            "DEEPSEEK_CACHE_ENABLED": "0",
            "DEEPSEEK_RETRY_COUNT": "0",
            "DEEPSEEK_STRATEGIES": "swing_picks",
        }
        rows = [
            {**self._rows()[0], "code": "000001", "score": 96},
            {**self._rows()[1], "code": "000002", "score": 94, "risk_penalty": 9},
            {**self._rows()[2], "code": "000003", "score": 80, "tier": "backup_pool"},
        ]
        with patch.dict(os.environ, env, clear=False), patch.object(deepseek_client, "_load_dotenv_if_needed"), patch(
            "stock_analyzer.deepseek_client.requests.post", return_value=response
        ) as post:
            _, meta = deepseek_client.rerank_candidates(rows, "swing_picks", model_tier_override="pro")

        self.assertEqual(meta["status"], "ok")
        self.assertEqual(meta["model_tier"], "pro")
        self.assertEqual(meta["review_limit"], 1)
        content = post.call_args.kwargs["json"]["messages"][1]["content"]
        self.assertIn('"code":"000002"', content)
        self.assertNotIn('"code":"000001"', content)
        self.assertNotIn('"code":"000003"', content)

    def test_pro_rerank_skips_api_when_no_boundary_samples(self):
        env = {
            "DEEPSEEK_ENABLED": "1",
            "DEEPSEEK_API_KEY": "test-key",
            "DEEPSEEK_CACHE_ENABLED": "0",
            "DEEPSEEK_RETRY_COUNT": "0",
            "DEEPSEEK_STRATEGIES": "swing_picks",
        }
        rows = [
            {**self._rows()[0], "code": "000001", "score": 96},
            {**self._rows()[1], "code": "000002", "score": 50},
            {**self._rows()[2], "code": "000003", "score": 47},
        ]
        with patch.dict(os.environ, env, clear=False), patch.object(deepseek_client, "_load_dotenv_if_needed"), patch(
            "stock_analyzer.deepseek_client.requests.post"
        ) as post:
            output_rows, meta = deepseek_client.rerank_candidates(rows, "swing_picks", model_tier_override="pro")

        self.assertEqual(output_rows, rows)
        self.assertEqual(meta["status"], "no_pro_boundary_samples")
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

    def test_news_context_attaches_recent_news_and_flags(self):
        from stock_analyzer import config

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = os.path.join(tmpdir, "news.json")
            news_items = [
                {"title": "样本A收到问询函并披露减持计划", "source": "测试", "publish_time": "2026-01-01"},
                {"title": "样本A签订订单", "source": "测试", "publish_time": "2026-01-01"},
            ]
            with patch.object(config, "ENABLE_DEEPSEEK_NEWS_CONTEXT", True), patch.object(
                config, "DEEPSEEK_NEWS_CACHE_PATH", cache_path
            ), patch.object(config, "NEWS_CACHE_HOURS", 6), patch.object(
                config, "DEEPSEEK_NEWS_CONTEXT_LIMIT", 2
            ), patch(
                "stock_analyzer.providers.MarketDataProvider.get_stock_news",
                return_value=news_items,
            ) as get_news:
                rows = deepseek_client._attach_news_context(
                    [
                        {
                            "code": "000001",
                            "name": "样本A",
                            "event_risk_flags": [{"label": "大额解禁"}],
                        }
                    ]
                )

        self.assertEqual(len(rows[0]["recent_news"]), 2)
        self.assertIn("问询函", rows[0]["announcement_flags"])
        self.assertIn("减持", rows[0]["announcement_flags"])
        self.assertIn("大额解禁", rows[0]["announcement_flags"])
        self.assertLess(rows[0]["news_sentiment"]["score"], 50)
        get_news.assert_called_once()

    def test_news_cache_prune_drops_old_entries_and_caps_size(self):
        now = 100000.0
        cache = {
            "old": {"fetched_at": now - 10000},
            "new1": {"fetched_at": now - 10},
            "new2": {"fetched_at": now - 20},
            "new3": {"fetched_at": now - 30},
            "bad": "invalid",
        }
        with patch.object(deepseek_client.time, "time", return_value=now), patch.object(
            deepseek_client.config, "NEWS_CACHE_HOURS", 1
        ), patch.object(deepseek_client.config, "DEEPSEEK_NEWS_CACHE_MAX_ENTRIES", 2):
            pruned, changed = deepseek_client._prune_news_cache(cache)

        self.assertTrue(changed)
        self.assertEqual(set(pruned.keys()), {"new1", "new2"})

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
        self.assertEqual(top["local_rank"], 2)
        self.assertTrue(top["deepseek_covered"])
        self.assertEqual(top["deepseek_blend_alpha"], 0.15)
        self.assertEqual(top["blend_alpha"], 0.15)
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

    def test_rerank_applies_oos_deepseek_rules_from_weights(self):
        response = MagicMock()
        response.status_code = 200
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {"results": [{"code": "000002", "llm_score": 98, "horizon_up_score": 98, "penalty": 0}]},
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
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "weights.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "deepseek_rules": {
                            "swing_picks": [
                                {"field": "pct_chg", "operator": ">", "threshold": 5, "penalty": 25, "reason": "OOS过热弱"}
                            ]
                        }
                    },
                    handle,
                    ensure_ascii=False,
                )
            input_rows = [
                {"code": "000001", "name": "样本A", "score": 80, "pct_chg": 2, "volume_ratio": 2, "turnover_rate": 5, "amplitude": 4},
                {"code": "000002", "name": "样本B", "score": 82, "pct_chg": 6, "volume_ratio": 2, "turnover_rate": 5, "amplitude": 4},
                {"code": "000003", "name": "样本C", "score": 70, "pct_chg": 1, "volume_ratio": 1.5, "turnover_rate": 4, "amplitude": 3},
            ]
            with patch.object(deepseek_client.config, "WEIGHTS_OVERRIDE_PATH", path), patch.dict(
                os.environ, env, clear=False
            ), patch.object(deepseek_client, "_load_dotenv_if_needed"), patch(
                "stock_analyzer.deepseek_client.requests.post", return_value=response
            ):
                rows, meta = deepseek_client.rerank_candidates(input_rows, "swing_picks")

        penalized = next(row for row in rows if row["code"] == "000002")
        self.assertEqual(meta["status"], "ok")
        self.assertEqual(penalized["deepseek_rule_penalty"], 25)
        self.assertEqual(penalized["deepseek_rules_matched"][0]["reason"], "OOS过热弱")

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

    def test_batch_rerank_calls_deepseek_once_for_multiple_strategies(self):
        response = MagicMock()
        response.status_code = 200
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "strategies": {
                                    "short_term": {"results": [{"code": "000001", "llm_score": 90, "horizon_up_score": 88}]},
                                    "tomorrow_picks": {"results": [{"code": "000002", "llm_score": 92, "horizon_up_score": 86}]},
                                }
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ],
            "usage": {"total_tokens": 30},
        }
        env = {
            "DEEPSEEK_ENABLED": "1",
            "DEEPSEEK_API_KEY": "test-key",
            "DEEPSEEK_CACHE_ENABLED": "0",
            "DEEPSEEK_RETRY_COUNT": "0",
        }
        with patch.dict(os.environ, env, clear=False), patch.object(deepseek_client, "_load_dotenv_if_needed"), patch(
            "stock_analyzer.deepseek_client.requests.post", return_value=response
        ) as post:
            rows_by_strategy = {
                "short_term": [
                    {**row, "score": 80 + index, "announcement_flags": ["订单"]}
                    for index, row in enumerate(self._rows())
                ],
                "tomorrow_picks": [
                    {**row, "score": 80 + index, "announcement_flags": ["减持"]}
                    for index, row in enumerate(self._rows())
                ],
            }
            rows, meta = deepseek_client.rerank_candidates_batch(
                rows_by_strategy,
                market_filter="all",
            )

        post.assert_called_once()
        request_payload = post.call_args.kwargs["json"]
        self.assertGreaterEqual(request_payload["max_tokens"], 1200)
        self.assertEqual(meta["short_term"]["source"], "deepseek_batch")
        self.assertEqual(meta["tomorrow_picks"]["source"], "deepseek_batch")
        self.assertIn("short_term", rows)
        self.assertIn("tomorrow_picks", rows)

    def test_batch_rerank_skips_alpha_zero_strategy_without_api_payload(self):
        env = {
            "DEEPSEEK_ENABLED": "1",
            "DEEPSEEK_API_KEY": "test-key",
            "DEEPSEEK_CACHE_ENABLED": "0",
            "DEEPSEEK_RETRY_COUNT": "0",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            weights_path = os.path.join(tmpdir, "weights.json")
            with open(weights_path, "w", encoding="utf-8") as handle:
                json.dump({"deepseek_blend_alpha": {"short_term": 0.0}}, handle)
            with patch.object(deepseek_client.config, "WEIGHTS_OVERRIDE_PATH", weights_path), patch.dict(
                os.environ, env, clear=False
            ), patch.object(deepseek_client, "_load_dotenv_if_needed"), patch(
                "stock_analyzer.deepseek_client.requests.post"
            ) as post:
                rows, meta = deepseek_client.rerank_candidates_batch({"short_term": self._rows()}, market_filter="all")

        self.assertEqual(meta["short_term"]["status"], "alpha_zero_skipped")
        self.assertEqual(rows["short_term"][0]["code"], "000001")
        post.assert_not_called()

    def test_cascade_filter_only_sends_flagged_ambiguous_rows(self):
        rows = [
            {"code": "A", "score": 94, "announcement_flags": []},
            {"code": "B", "score": 78, "announcement_flags": ["减持"]},
            {"code": "C", "score": 70, "recent_news": [{"title": "订单增长"}]},
            {"code": "D", "score": 40, "announcement_flags": ["问询函"]},
        ]
        selected, meta = deepseek_client._select_batch_review_pool(
            rows,
            10,
            {"cascade_filter_enabled": True, "cascade_max_review": 8},
        )

        self.assertEqual([row["code"] for row in selected], ["B", "C"])
        self.assertEqual(meta["skipped_local_confident"], 2)

    def test_request_payload_compresses_news_and_drops_redundant_fields(self):
        payload = deepseek_client._request_payload(
            "tomorrow_picks",
            [
                {
                    "code": "600001",
                    "name": "样本",
                    "score": 82.4,
                    "pct_chg": 3.234,
                    "ytd_pct": 99,
                    "market_label": "主板",
                    "recent_news": [
                        {"title": "这是一条非常长的新闻标题" * 8, "source": "测试媒体", "publish_time": "2026-01-01 09:30:00"},
                        {"title": "第二条新闻", "source": "测试媒体", "publish_time": "2026-01-01"},
                        {"title": "第三条新闻", "source": "测试媒体", "publish_time": "2026-01-01"},
                        {"title": "第四条不应进入", "source": "测试媒体", "publish_time": "2026-01-01"},
                    ],
                }
            ],
            "all",
        )[0]

        self.assertNotIn("ytd_pct", payload)
        self.assertNotIn("market_label", payload)
        self.assertEqual(payload["score"], 82)
        self.assertEqual(payload["pct_chg"], 3.2)
        self.assertEqual(len(payload["recent_news"]), 3)
        self.assertLessEqual(len(payload["recent_news"][0]["title"]), 60)

    def test_market_gate_review_parses_size_factor(self):
        from stock_analyzer import config

        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {"regime": "risk_off", "size_factor": 0.35, "confidence": 82, "reason": "宽度弱"},
                            ensure_ascii=False,
                        )
                    }
                }
            ],
            "usage": {"total_tokens": 20},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = os.path.join(tmpdir, "gate.json")
            env = {
                "DEEPSEEK_ENABLED": "1",
                "DEEPSEEK_API_KEY": "test-key",
                "DEEPSEEK_CACHE_ENABLED": "0",
            }
            with patch.object(config, "ENABLE_DEEPSEEK_MARKET_GATE", True), patch.object(
                config, "DEEPSEEK_MARKET_GATE_CACHE_PATH", cache_path
            ), patch.dict(os.environ, env, clear=False), patch.object(
                deepseek_client, "_load_dotenv_if_needed"
            ), patch(
                "stock_analyzer.deepseek_client.requests.post", return_value=response
            ) as post:
                result = deepseek_client.review_market_regime({"up_ratio_pct": 45, "limit_up_count": 2, "avg_pct_chg": 0.0})

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["regime"], "risk_off")
        self.assertEqual(result["size_factor"], 0.35)
        self.assertEqual(result["confidence"], 82)
        post.assert_called_once()

    def test_market_gate_uses_local_decisive_result_without_api_call(self):
        from stock_analyzer import config

        with patch.object(config, "ENABLE_DEEPSEEK_MARKET_GATE", True), patch(
            "stock_analyzer.deepseek_client.requests.post"
        ) as post:
            result = deepseek_client.review_market_regime({"up_ratio_pct": 20, "limit_up_count": 2, "avg_pct_chg": -1.5})

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["regime"], "risk_off")
        self.assertEqual(result["decision_path"], "local_decisive")
        post.assert_not_called()

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
        self.assertEqual(payload["deepseek_review"]["status"], "not_requested")
        review.assert_not_called()

    def test_strategy_validation_get_caches_deepseek_summary(self):
        from stock_analyzer import config
        from stock_analyzer.app import create_app

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "validation.sqlite3")
            with patch.object(config, "VALIDATION_DB_PATH", db_path), patch.object(config, "REFRESH_SECONDS", 60), patch(
                "stock_analyzer.strategy_validation.StrategyValidationStore.deepseek_attribution",
                return_value={"status": "ok"},
            ) as attribution, patch(
                "stock_analyzer.strategy_validation.StrategyValidationStore.market_gate_metrics",
                return_value={"status": "ok"},
            ) as gate:
                app = create_app()
                app.config["TESTING"] = True
                client = app.test_client()
                first = client.get("/api/strategy-validation?strategy=tomorrow_picks&days=1")
                second = client.get("/api/strategy-validation?strategy=tomorrow_picks&days=1")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(attribution.call_count, len(config.SNAPSHOT_STRATEGIES))
        gate.assert_called_once()

    def test_stock_prediction_deepseek_is_manual_opt_in(self):
        from stock_analyzer import config
        from stock_analyzer.app import create_app

        quotes = pd.DataFrame(
            [
                {
                    "code": "600001",
                    "name": "样本",
                    "price": 10.0,
                    "pct_chg": 1.0,
                    "volume_ratio": 1.2,
                    "turnover_rate": 3.0,
                    "turnover": 300000000,
                    "industry": "测试",
                    "sixty_day_pct": 5.0,
                    "ytd_pct": 8.0,
                }
            ]
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(config, "STATE_PATH", os.path.join(tmpdir, "state.json")), patch.object(
                config, "VALIDATION_DB_PATH", os.path.join(tmpdir, "validation.sqlite3")
            ), patch.object(config, "ENABLE_HISTORY_FACTORS", False), patch(
                "stock_analyzer.app.MarketDataProvider.get_realtime_quotes",
                return_value=quotes,
            ), patch(
                "stock_analyzer.app.deepseek_stock_prediction_review",
                return_value={"enabled": True, "status": "ok", "summary": "test"},
            ) as review:
                app = create_app()
                app.config["TESTING"] = True
                client = app.test_client()
                normal = client.get("/api/stock-prediction/600001")
                explicit = client.get("/api/stock-prediction/600001?deepseek=1")

        self.assertEqual(normal.status_code, 200)
        self.assertNotIn("optimization", normal.get_json())
        self.assertEqual(explicit.status_code, 200)
        self.assertEqual(explicit.get_json()["optimization"]["status"], "ok")
        review.assert_called_once()

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

import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

from stock_analyzer import config
from stock_analyzer.app import create_app
from stock_analyzer.backtest import run_alphalite_backtest, run_rolling_alphalite_backtest
from stock_analyzer.factors import compute_alphalite_for_stock
from stock_analyzer.history_cache import HistoryCache
from stock_analyzer.normalization import rename_known_columns
from stock_analyzer.providers import MarketDataProvider, _normalize_eastmoney_spot, _request_eastmoney_page
from stock_analyzer.scoring import (
    build_market_regime,
    build_strategy_consensus,
    prepare_candidates,
    score_candidates,
    score_chokepoint_candidates,
    score_reversal_candidates,
    score_smallcap_value_candidates,
    score_breakout_candidates,
    score_dual_horizon_candidates,
    score_position_candidates,
    score_swing_candidates,
    score_tech_potential_candidates,
    score_tomorrow_candidates,
)
from stock_analyzer.sentiment import score_news_items
from stock_analyzer.stability import TopKDropoutTracker
from stock_analyzer.strategy_validation import StrategyValidationStore
from stock_analyzer.validation_replay import backfill_strategy_validation_samples


class ScoringTest(unittest.TestCase):
    def assertHasExplanationFields(self, row, strategy_name):
        self.assertEqual(row["strategy_name"], strategy_name)
        self.assertIn("strategy_label", row)
        self.assertIn("signal_label", row)
        self.assertIn("chase_risk", row)
        self.assertIn(row["chase_risk"]["level"], {"low", "medium", "high"})
        self.assertIn("overextension", row)
        self.assertIn(row["overextension"]["level"], {"low", "medium", "high"})
        self.assertIn("failure_reasons", row)
        self.assertTrue(row["failure_reasons"])
        self.assertIn("agent_committee", row)
        self.assertEqual(row["agent_committee"]["version"], "trading_agents_committee_v1")
        self.assertIn(row["agent_committee"]["stance"], {"approve", "small_position", "wait", "reject"})
        self.assertIn("final_score", row["agent_committee"])
        self.assertIn("risk_manager_score", row["agent_committee"])
        self.assertIn("portfolio_manager_score", row["agent_committee"])
        self.assertTrue(row["agent_committee"]["bull_cases"])
        self.assertTrue(row["agent_committee"]["bear_cases"])
        self.assertIn("serenity_profile", row)
        self.assertEqual(row["serenity_profile"]["version"], "serenity_profile_v1")
        self.assertIn(row["serenity_profile"]["level"], {"good", "risk", "watch", "neutral"})
        self.assertIn("quality_score", row["serenity_profile"])
        self.assertIn("risk_score", row["serenity_profile"])
        self.assertIn("confidence_score", row["serenity_profile"])
        self.assertIn("agent_committee_score", row["serenity_profile"])

    def test_prepare_candidates_keeps_star_market_and_filters_st(self):
        quotes = pd.DataFrame(
            [
                {"code": "688001", "name": "科创样本", "price": 20, "pct_chg": 6, "turnover": 90000000},
                {"code": "300001", "name": "创业样本", "price": 10, "pct_chg": 4, "turnover": 80000000},
                {"code": "430001", "name": "北交样本", "price": 10, "pct_chg": 4, "turnover": 80000000},
                {"code": "600001", "name": "ST样本", "price": 10, "pct_chg": 4, "turnover": 80000000},
            ]
        )

        result = prepare_candidates(quotes)

        self.assertEqual(set(result["code"]), {"688001", "300001"})
        self.assertEqual(result[result["code"] == "688001"].iloc[0]["market"], "star")

    def test_prepare_candidates_filters_near_limit_up_unbuyable_names(self):
        quotes = pd.DataFrame(
            [
                {"code": "600001", "name": "主板可买", "price": 10, "pct_chg": 6.5, "turnover": 90000000},
                {"code": "600002", "name": "主板过高", "price": 10, "pct_chg": 9.2, "turnover": 90000000},
                {"code": "300001", "name": "创业可买", "price": 10, "pct_chg": 9.5, "turnover": 90000000},
                {"code": "300002", "name": "创业过高", "price": 10, "pct_chg": 18.5, "turnover": 90000000},
            ]
        )

        result = prepare_candidates(quotes)

        self.assertEqual(set(result["code"]), {"600001", "300001"})

    def test_score_candidates_orders_by_combined_signal(self):
        quotes = pd.DataFrame(
            [
                {
                    "code": "600001",
                    "name": "强势样本",
                    "price": 12,
                    "pct_chg": 6,
                    "speed": 2,
                    "volume_ratio": 3,
                    "turnover_rate": 8,
                    "turnover": 300000000,
                    "industry": "半导体",
                },
                {
                    "code": "600002",
                    "name": "普通样本",
                    "price": 10,
                    "pct_chg": 2,
                    "speed": 0.2,
                    "volume_ratio": 1,
                    "turnover_rate": 2,
                    "turnover": 60000000,
                    "industry": "银行",
                },
            ]
        )
        candidates = prepare_candidates(quotes)

        rows, _ = score_candidates(
            candidates,
            hot_ranks={"600001": 10},
            industry_strength={"半导体": 2.5, "银行": -0.2},
            sentiment_lookup={"600001": {"score": 70, "summary": "舆情偏正面"}},
            top_n=2,
        )

        self.assertEqual(rows[0]["code"], "600001")
        self.assertGreater(rows[0]["score"], rows[1]["score"])

    def test_sentiment_scores_positive_and_negative_words(self):
        positive = score_news_items([{"title": "公司中标大订单", "content": "", "publish_time": ""}])
        negative = score_news_items([{"title": "公司被立案调查并收到处罚", "content": "", "publish_time": ""}])

        self.assertGreater(positive["score"], 50)
        self.assertLess(negative["score"], 50)
        self.assertIn("立案", negative["risk_words"])

    def test_dual_horizon_returns_short_and_long_top_10(self):
        quotes = pd.DataFrame(
            [
                {
                    "code": "600001",
                    "name": "短线样本",
                    "price": 12,
                    "pct_chg": 6,
                    "speed": 2.2,
                    "volume_ratio": 3,
                    "turnover_rate": 9,
                    "turnover": 360000000,
                    "industry": "半导体",
                    "sixty_day_pct": 4,
                    "ytd_pct": 5,
                    "amplitude": 8,
                },
                {
                    "code": "600002",
                    "name": "长线样本",
                    "price": 18,
                    "pct_chg": 2,
                    "speed": 0.3,
                    "volume_ratio": 1.4,
                    "turnover_rate": 4,
                    "turnover": 420000000,
                    "industry": "电力",
                    "sixty_day_pct": 28,
                    "ytd_pct": 35,
                    "amplitude": 3,
                },
            ]
        )
        candidates = prepare_candidates(quotes)

        result, meta = score_dual_horizon_candidates(
            candidates,
            hot_ranks={"600001": 8},
            industry_strength={"半导体": 1.2, "电力": 1.5},
            sentiment_lookup={"600002": {"score": 68, "summary": "舆情偏正面"}},
            top_n=10,
        )

        self.assertIn("short_term", result)
        self.assertIn("long_term", result)
        self.assertEqual(meta["top_n"], 10)
        self.assertEqual(result["short_term"][0]["code"], "600001")
        self.assertEqual(result["long_term"][0]["code"], "600002")
        self.assertHasExplanationFields(result["short_term"][0], "short_term")
        self.assertHasExplanationFields(result["long_term"][0], "long_term")

    def test_build_market_regime_identifies_risk_on_environment(self):
        quotes = pd.DataFrame(
            [
                {"code": "600001", "name": "样本1", "price": 10, "pct_chg": 6.2, "turnover": 500000000, "amplitude": 5},
                {"code": "600002", "name": "样本2", "price": 12, "pct_chg": 4.8, "turnover": 430000000, "amplitude": 6},
                {"code": "300001", "name": "样本3", "price": 18, "pct_chg": 7.5, "turnover": 620000000, "amplitude": 7},
                {"code": "688001", "name": "样本4", "price": 25, "pct_chg": 5.4, "turnover": 700000000, "amplitude": 6},
            ]
        )

        regime = build_market_regime(prepare_candidates(quotes))

        self.assertEqual(regime["level"], "risk_on")
        self.assertGreater(regime["score"], 60)
        self.assertTrue(regime["leaders"])

    def test_build_strategy_consensus_collects_multi_strategy_overlap(self):
        rows = build_strategy_consensus(
            {
                "short_term": [{"code": "600001", "name": "共识样本", "rank": 1, "score": 92, "market_label": "主板"}],
                "long_term": [
                    {
                        "code": "600001",
                        "name": "共识样本",
                        "rank": 3,
                        "score": 88,
                        "market_label": "主板",
                        "serenity_profile": {"quality_score": 82, "risk_score": 30, "confidence_score": 75},
                        "agent_committee": {"final_score": 70, "final_action_label": "交易员小仓试单"},
                    }
                ],
                "tomorrow_picks": [
                    {
                        "code": "600001",
                        "name": "共识样本",
                        "rank": 2,
                        "score": 85,
                        "market_label": "主板",
                        "serenity_profile": {
                            "quality_score": 80,
                            "risk_score": 35,
                            "confidence_score": 72,
                            "evidence": [{"label": "动量强"}],
                            "action_label": "优先跟踪",
                        },
                        "agent_committee": {
                            "final_score": 78,
                            "final_action_label": "组合经理批准",
                        },
                    }
                ],
                "tech_potential": [{"code": "600002", "name": "非共识", "rank": 1, "score": 90, "market_label": "主板"}],
            },
            minimum_appearances=2,
            top_n=10,
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["code"], "600001")
        self.assertEqual(rows[0]["appearances"], 3)
        self.assertIn("短期推荐", rows[0]["strategies"])
        self.assertIn("avg_quality", rows[0])
        self.assertIn("avg_risk", rows[0])
        self.assertIn("avg_agent_score", rows[0])
        self.assertIn("组合经理批准", rows[0]["agent_actions"])
        self.assertIn("action_label", rows[0])
        self.assertIn("动量强", rows[0]["evidence"])

    def test_build_strategy_consensus_can_keep_thirty_rows(self):
        base_rows = []
        second_rows = []
        for index in range(35):
            code = "600{:03d}".format(index + 1)
            base_rows.append(
                {
                    "code": code,
                    "name": "共识{}".format(index + 1),
                    "rank": index + 1,
                    "score": 90 - index * 0.2,
                    "market_label": "主板",
                    "serenity_profile": {"quality_score": 75, "risk_score": 35, "confidence_score": 70},
                    "agent_committee": {"final_score": 72, "final_action_label": "组合经理批准"},
                }
            )
            second_rows.append(
                {
                    "code": code,
                    "name": "共识{}".format(index + 1),
                    "rank": index + 2,
                    "score": 88 - index * 0.2,
                    "market_label": "主板",
                    "serenity_profile": {"quality_score": 73, "risk_score": 38, "confidence_score": 68},
                    "agent_committee": {"final_score": 70, "final_action_label": "交易员小仓试单"},
                }
            )

        rows = build_strategy_consensus(
            {"short_term": base_rows, "tomorrow_picks": second_rows},
            minimum_appearances=2,
            top_n=30,
        )

        self.assertEqual(len(rows), 30)
        self.assertEqual(rows[0]["code"], "600001")

    def test_verdict_tier_bands_and_coverage_gate(self):
        from stock_analyzer.scoring import _verdict_tier

        # 高分低风险 + 覆盖充足 → strong_buy
        self.assertEqual(_verdict_tier(85, 30, 0.9)["tier"], "strong_buy")
        # 低分 → avoid
        self.assertEqual(_verdict_tier(20, 40, 0.9)["tier"], "avoid")
        # A4：高分但数据覆盖不足 → 强制降级到 watch，且带 note
        gated = _verdict_tier(85, 30, 0.2)
        self.assertEqual(gated["tier"], "watch")
        self.assertTrue(gated["note"])

    def test_consensus_stretch_rewards_agreement(self):
        from stock_analyzer.scoring import _consensus_stretch

        # 同一基础分，高一致性应被拉得比低一致性更高（>50 区间）。
        high = _consensus_stretch(80, 1.0)
        low = _consensus_stretch(80, 0.0)
        self.assertGreater(high, low)
        self.assertGreater(high, 80)
        self.assertLess(low, 80)

    def test_overheat_damp_suppresses_extended_names(self):
        from stock_analyzer.scoring import _apply_overheat_damp

        calm = pd.Series({"sixty_day_pct": 5, "ytd_pct": 10, "amplitude": 4})
        extended = pd.Series({"sixty_day_pct": 130, "ytd_pct": 160, "amplitude": 15})
        # 过热票的 final 被乘法压低，明显低于温和票。
        self.assertLess(_apply_overheat_damp(80, extended), _apply_overheat_damp(80, calm))
        self.assertLessEqual(_apply_overheat_damp(80, extended), 80)

    def test_chokepoint_score_rewards_upstream_underpriced(self):
        from stock_analyzer.scoring import _chokepoint_score

        upstream = pd.Series({"name": "某光模块", "industry": "光器件", "sixty_day_pct": 8})
        score, hits = _chokepoint_score(upstream)
        self.assertTrue(hits)
        self.assertGreater(score, 60)
        # 无上游关键词 → 中性 50、无命中。
        neutral_score, neutral_hits = _chokepoint_score(
            pd.Series({"name": "某银行", "industry": "银行", "sixty_day_pct": 5})
        )
        self.assertEqual(neutral_hits, [])
        self.assertEqual(neutral_score, 50.0)

    def test_strategy_reliability_from_win_rate(self):
        from stock_analyzer.scoring import _strategy_reliability

        rel = _strategy_reliability(
            {
                "a": {"sample_count": 18, "win_rate_next_close": 70, "avg_next_close_return": 1.0},
                "b": {"sample_count": 18, "win_rate_next_close": 30, "avg_next_close_return": -1.0},
                "c": {},  # 无命中率 → 不产生乘子
            }
        )
        self.assertGreater(rel["a"], 1.0)
        self.assertLess(rel["b"], 1.0)
        self.assertNotIn("c", rel)

    def test_strategy_reliability_prefers_real_forward_samples(self):
        from stock_analyzer.scoring import _strategy_reliability

        rel = _strategy_reliability(
            {
                "real_good_replay_bad": {
                    "sample_count": 80,
                    "real_sample_count": 12,
                    "replay_sample_count": 68,
                    "real_win_rate_primary_net": 62,
                    "real_avg_primary_return_net": 1.2,
                    "win_rate_primary_net": 38,
                    "avg_primary_return_net": -1.5,
                },
                "real_bad_replay_good": {
                    "sample_count": 80,
                    "real_sample_count": 12,
                    "replay_sample_count": 68,
                    "real_win_rate_primary_net": 42,
                    "real_avg_primary_return_net": -0.8,
                    "win_rate_primary_net": 72,
                    "avg_primary_return_net": 2.5,
                },
            }
        )

        self.assertGreater(rel["real_good_replay_bad"], 1.0)
        self.assertLess(rel["real_bad_replay_good"], 1.0)

    def test_serenity_references_corrected_to_chokepoint(self):
        from stock_analyzer.scoring import SERENITY_REFERENCES

        joined = " ".join(ref.get("adopted", "") + ref.get("repo", "") for ref in SERENITY_REFERENCES)
        # 不再误标为 BDD/Discord/OS 等不相关仓库
        self.assertNotIn("serenity-bdd", joined)
        self.assertNotIn("SerenityOS", joined)
        # 体现卡脖子/瓶颈方法论
        self.assertIn("卡脖子", joined)

    def test_weights_override_loads_from_json(self):
        import json
        import tempfile
        import importlib

        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/weights.json"
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"thresholds": {"min_data_coverage": 0.99}}, handle)
            with patch.object(config, "WEIGHTS_OVERRIDE_PATH", path):
                import stock_analyzer.scoring as scoring

                weights, thresholds = scoring._load_weight_overrides()
                self.assertEqual(thresholds["min_data_coverage"], 0.99)
            # 不存在文件时回退默认
            with patch.object(config, "WEIGHTS_OVERRIDE_PATH", f"{tmp}/missing.json"):
                weights, thresholds = scoring._load_weight_overrides()
                self.assertEqual(thresholds["min_data_coverage"], 0.5)

    def test_dual_horizon_rows_carry_verdict_and_bull_bear(self):
        quotes = pd.DataFrame(
            [
                {"code": "600001", "name": "动量样本", "price": 12, "pct_chg": 3.2, "turnover": 9e8,
                 "turnover_rate": 6, "volume_ratio": 2.1, "speed": 1.2, "sixty_day_pct": 18, "ytd_pct": 30,
                 "amplitude": 5},
                {"code": "300002", "name": "趋势样本", "price": 20, "pct_chg": 1.1, "turnover": 7e8,
                 "turnover_rate": 4, "volume_ratio": 1.4, "speed": 0.4, "sixty_day_pct": 12, "ytd_pct": 22,
                 "amplitude": 4},
            ]
        )
        candidates = prepare_candidates(quotes)
        result, _ = score_dual_horizon_candidates(candidates, {}, {}, {}, top_n=10)
        short_rows = result["short_term"]
        self.assertTrue(short_rows)
        row = short_rows[0]
        self.assertIn("verdict", row)
        self.assertIn(row["verdict"]["tier"], {"strong_buy", "buy", "watch", "reduce", "avoid"})
        self.assertIn("bull_score", row)
        self.assertIn("bear_score", row)

    def test_bear_score_defaults_neutral_when_committee_missing(self):
        from stock_analyzer.scoring import _attach_signal_explanation

        item = {"code": "600001", "name": "样本", "score": 60.0}
        row = pd.Series({"code": "600001", "name": "样本", "pct_chg": 2.0, "market": "main"})
        out = _attach_signal_explanation(item, row, "short_term", "短线", "盘中强势")
        # 即使委员会缺失，bear_score 也应回退到中性 50（而非 0）。
        self.assertIsInstance(out["bear_score"], (int, float))
        self.assertGreaterEqual(out["bull_score"], 0)

    def test_weights_override_rejects_unsafe_scalar(self):
        import json
        import tempfile

        cases = [
            {"thresholds": {"consensus_stretch_k": 0}},
            {"thresholds": {"verdict": 80}},
            {"thresholds": {"min_data_coverage": -1}},
        ]
        for bad in cases:
            with tempfile.TemporaryDirectory() as tmp:
                path = f"{tmp}/weights.json"
                with open(path, "w", encoding="utf-8") as handle:
                    json.dump(bad, handle)
                with patch.object(config, "WEIGHTS_OVERRIDE_PATH", path):
                    import stock_analyzer.scoring as scoring

                    _, thresholds = scoring._load_weight_overrides()
                    self.assertGreater(thresholds["consensus_stretch_k"], 0)
                    self.assertIsInstance(thresholds["verdict"], dict)
                    self.assertTrue(0.0 <= thresholds["min_data_coverage"] <= 1.0)

    def test_chokepoint_candidates_filter_and_chain(self):
        quotes = pd.DataFrame(
            [
                {"code": "300308", "name": "光模块龙头", "price": 100, "pct_chg": 2.1, "turnover": 9e8,
                 "turnover_rate": 6, "volume_ratio": 2.0, "speed": 1.0, "sixty_day_pct": 15, "ytd_pct": 30,
                 "amplitude": 5, "industry": "光器件"},
                {"code": "002916", "name": "某载板", "price": 50, "pct_chg": 1.5, "turnover": 7e8,
                 "turnover_rate": 5, "volume_ratio": 1.5, "speed": 0.5, "sixty_day_pct": 10, "ytd_pct": 20,
                 "amplitude": 4, "industry": "封装基板"},
                {"code": "600000", "name": "某银行", "price": 10, "pct_chg": 0.5, "turnover": 5e8,
                 "turnover_rate": 2, "volume_ratio": 1.0, "speed": 0.1, "sixty_day_pct": 3, "ytd_pct": 5,
                 "amplitude": 2, "industry": "银行"},
            ]
        )
        candidates = prepare_candidates(quotes)
        rows, meta = score_chokepoint_candidates(candidates, top_n=10)
        codes = {r["code"] for r in rows}
        # 银行不命中卡脖子关键词，应被过滤；上游票应保留并带 chain_segment + verdict。
        self.assertNotIn("600000", codes)
        self.assertTrue(rows)
        for r in rows:
            self.assertIn("chain_segment", r)
            self.assertIn("chokepoint_score", r)
            self.assertIn("verdict", r)
        # meta.chain 应是按环节分组的列表，且至少一个环节有 picks。
        self.assertIn("chain", meta)
        self.assertTrue(any(node["count"] > 0 for node in meta["chain"]))
        quotes = pd.DataFrame(
            [
                {
                    "code": "600001",
                    "name": "可买强势",
                    "price": 12,
                    "pct_chg": 5.2,
                    "volume_ratio": 2.1,
                    "turnover_rate": 5,
                    "turnover": 800000000,
                    "sixty_day_pct": 18,
                    "ytd_pct": 25,
                    "amplitude": 6,
                },
                {
                    "code": "600002",
                    "name": "接近涨停",
                    "price": 10,
                    "pct_chg": 9.5,
                    "volume_ratio": 3.5,
                    "turnover_rate": 8,
                    "turnover": 900000000,
                    "sixty_day_pct": 20,
                    "ytd_pct": 30,
                    "amplitude": 8,
                },
                {
                    "code": "300001",
                    "name": "创业可买",
                    "price": 20,
                    "pct_chg": 8.5,
                    "volume_ratio": 2.5,
                    "turnover_rate": 7,
                    "turnover": 700000000,
                    "sixty_day_pct": 16,
                    "ytd_pct": 22,
                    "amplitude": 7,
                },
            ]
        )
        candidates = prepare_candidates(quotes)

        rows, meta = score_tomorrow_candidates(candidates, top_n=50)

        self.assertEqual({row["code"] for row in rows}, {"600001", "300001"})
        self.assertEqual(meta["analysis_window"], "14:30")
        self.assertLessEqual(len(rows), 50)
        self.assertHasExplanationFields(rows[0], "tomorrow_picks")

    def test_reversal_candidates_prefer_oversold_low_vol(self):
        quotes = pd.DataFrame(
            [
                {"code": "600001", "name": "超跌低波", "price": 8, "pct_chg": -0.5, "turnover": 6e8,
                 "turnover_rate": 3, "volume_ratio": 1.1, "speed": -0.2, "sixty_day_pct": -25, "ytd_pct": -30,
                 "amplitude": 4, "ret_20d": -22, "volatility_20d": 2.0, "industry": "机械"},
                {"code": "600002", "name": "强势高波", "price": 20, "pct_chg": 3.5, "turnover": 9e8,
                 "turnover_rate": 9, "volume_ratio": 2.5, "speed": 1.5, "sixty_day_pct": 22, "ytd_pct": 28,
                 "amplitude": 8, "ret_20d": 20, "volatility_20d": 5.0, "industry": "电子"},
            ]
        )
        candidates = prepare_candidates(quotes)
        rows, meta = score_reversal_candidates(candidates, top_n=10)
        self.assertTrue(rows)
        # 超跌低波票应排在强势高波票前面（反转逻辑）。
        self.assertEqual(rows[0]["code"], "600001")
        self.assertIn("verdict", rows[0])
        self.assertIn("reversal_score", rows[0])

    def test_smallcap_value_guards_filter_loss_and_tiny(self):
        quotes = pd.DataFrame(
            [
                {"code": "300003", "name": "小盘低估", "price": 12, "pct_chg": 0.8, "turnover": 7e8,
                 "turnover_rate": 4, "volume_ratio": 1.3, "speed": 0.3, "sixty_day_pct": 5, "ytd_pct": 8,
                 "amplitude": 3, "float_market_cap": 2.5e9, "pe_dynamic": 12, "pb": 1.1, "industry": "化工"},
                {"code": "600002", "name": "亏损股", "price": 20, "pct_chg": 1.0, "turnover": 7e8,
                 "turnover_rate": 4, "volume_ratio": 1.3, "speed": 0.3, "sixty_day_pct": 5, "ytd_pct": 8,
                 "amplitude": 3, "float_market_cap": 5e9, "pe_dynamic": -8, "pb": 2.0, "industry": "化工"},
                {"code": "600003", "name": "巨型蓝筹", "price": 30, "pct_chg": 0.5, "turnover": 7e8,
                 "turnover_rate": 4, "volume_ratio": 1.3, "speed": 0.3, "sixty_day_pct": 5, "ytd_pct": 8,
                 "amplitude": 3, "float_market_cap": 9e11, "pe_dynamic": 30, "pb": 5.0, "industry": "化工"},
            ]
        )
        candidates = prepare_candidates(quotes)
        rows, meta = score_smallcap_value_candidates(candidates, top_n=10)
        codes = {r["code"] for r in rows}
        # 亏损股(PE<=0)被护栏过滤；小盘低估应入选且排第一。
        self.assertNotIn("600002", codes)
        self.assertTrue(rows)
        self.assertEqual(rows[0]["code"], "300003")
        self.assertIn("risk_note", meta)

    def test_breakout_requires_alignment_or_newhigh(self):
        quotes = pd.DataFrame(
            [
                {"code": "600001", "name": "突破多头", "price": 20, "pct_chg": 3.0, "turnover": 9e8,
                 "turnover_rate": 7, "volume_ratio": 2.2, "speed": 1.5, "sixty_day_pct": 22, "ytd_pct": 28,
                 "amplitude": 5, "ma_bull_aligned": 1, "breakout_20d": 1, "vol_ma5_ratio": 2.1, "ma20_gap": 9,
                 "industry": "电子"},
                {"code": "600002", "name": "无突破", "price": 10, "pct_chg": 0.5, "turnover": 6e8,
                 "turnover_rate": 3, "volume_ratio": 1.0, "speed": 0.1, "sixty_day_pct": 2, "ytd_pct": 3,
                 "amplitude": 2, "ma_bull_aligned": 0, "breakout_20d": 0, "vol_ma5_ratio": 0.9, "ma20_gap": -3,
                 "industry": "电子"},
            ]
        )
        candidates = prepare_candidates(quotes)
        rows, meta = score_breakout_candidates(candidates, top_n=10)
        codes = {r["code"] for r in rows}
        # 无多头排列且无新高的票应被预过滤剔除。
        self.assertIn("600001", codes)
        self.assertNotIn("600002", codes)

    def test_prepare_candidates_coerces_fundamental_fields(self):
        quotes = pd.DataFrame(
            [{"code": "600001", "name": "x", "price": 10, "pct_chg": 2, "turnover": 9e8,
              "float_market_cap": 5e9, "market_cap": 6e9, "pe_dynamic": 18, "pb": 2.1}]
        )
        result = prepare_candidates(quotes)
        for col in ("float_market_cap", "market_cap", "pe_dynamic", "pb"):
            self.assertIn(col, result.columns)
        self.assertEqual(float(result.iloc[0]["pe_dynamic"]), 18.0)

    def test_factors_compute_ma_alignment_and_volume(self):
        n = 70
        trend = pd.DataFrame({
            "trade_date": [f"d{i}" for i in range(n)], "code": "600001",
            "open": range(1, n + 1), "high": [x * 1.01 for x in range(1, n + 1)],
            "low": [x * 0.99 for x in range(1, n + 1)], "price": [float(x) for x in range(1, n + 1)],
            "turnover": [1e8] * n, "volume": [1e6] * (n - 1) + [3e6],
        })
        f = compute_alphalite_for_stock("600001", trend)
        self.assertEqual(f["ma_bull_aligned"], 1.0)
        self.assertGreater(f["vol_ma5_ratio"], 1.5)
        self.assertIn("ma60_gap", f)
        self.assertIn("ma10_gap", f)

    def test_tech_potential_prefers_theme_match_without_overextension(self):
        quotes = pd.DataFrame(
            [
                {
                    "code": "600001",
                    "name": "芯片科技",
                    "price": 12,
                    "pct_chg": 3.2,
                    "volume_ratio": 1.8,
                    "turnover_rate": 4,
                    "turnover": 700000000,
                    "sixty_day_pct": 18,
                    "ytd_pct": 32,
                    "amplitude": 5,
                },
                {
                    "code": "600002",
                    "name": "芯片高位",
                    "price": 20,
                    "pct_chg": 6.8,
                    "volume_ratio": 5.8,
                    "turnover_rate": 12,
                    "turnover": 900000000,
                    "sixty_day_pct": 88,
                    "ytd_pct": 140,
                    "amplitude": 11,
                },
                {
                    "code": "600003",
                    "name": "传统消费",
                    "price": 8,
                    "pct_chg": 3.5,
                    "volume_ratio": 1.6,
                    "turnover_rate": 3,
                    "turnover": 800000000,
                    "sixty_day_pct": 10,
                    "ytd_pct": 15,
                    "amplitude": 4,
                },
            ]
        )
        candidates = prepare_candidates(quotes)

        rows, meta = score_tech_potential_candidates(candidates, top_n=50)

        self.assertEqual(rows[0]["code"], "600001")
        self.assertNotIn("600003", {row["code"] for row in rows})
        self.assertGreater(meta["matched_count"], 0)
        self.assertHasExplanationFields(rows[0], "tech_potential")

    def test_swing_candidates_prefer_5_10_day_momentum_without_heat(self):
        quotes = pd.DataFrame(
            [
                {
                    "code": "600001",
                    "name": "波段温和",
                    "price": 12,
                    "pct_chg": 3.2,
                    "volume_ratio": 2.0,
                    "turnover_rate": 5,
                    "turnover": 700000000,
                    "sixty_day_pct": 22,
                    "ytd_pct": 35,
                    "amplitude": 5,
                    "ret_5d": 5,
                    "ret_10d": 9,
                    "ret_20d": 15,
                    "ma5_gap": 3,
                    "ma20_gap": 7,
                    "vol_amount_5d": 1.4,
                    "breakout_20d": 1,
                    "volatility_20d": 2.5,
                },
                {
                    "code": "600002",
                    "name": "波段过热",
                    "price": 18,
                    "pct_chg": 7.8,
                    "volume_ratio": 6.5,
                    "turnover_rate": 22,
                    "turnover": 800000000,
                    "sixty_day_pct": 82,
                    "ytd_pct": 125,
                    "amplitude": 11,
                    "ret_5d": 18,
                    "ret_10d": 35,
                    "ret_20d": 52,
                    "ma5_gap": 20,
                    "ma20_gap": 40,
                    "vol_amount_5d": 3.5,
                    "breakout_20d": 1,
                    "volatility_20d": 9,
                },
            ]
        )
        candidates = prepare_candidates(quotes)

        rows, meta = score_swing_candidates(candidates, top_n=30)

        self.assertEqual(rows[0]["code"], "600001")
        self.assertEqual(meta["strategy_version"], "swing_5_10d_v1")
        self.assertEqual(rows[0]["horizon"], "swing")
        self.assertHasExplanationFields(rows[0], "swing_picks")

    def test_position_candidates_filter_overextended_and_mark_limitation(self):
        quotes = pd.DataFrame(
            [
                {
                    "code": "688001",
                    "name": "芯片稳健",
                    "price": 30,
                    "pct_chg": 2.0,
                    "volume_ratio": 1.6,
                    "turnover_rate": 4,
                    "turnover": 900000000,
                    "sixty_day_pct": 30,
                    "ytd_pct": 48,
                    "amplitude": 4,
                    "ret_10d": 6,
                    "ret_20d": 14,
                    "ma20_gap": 6,
                    "vol_amount_5d": 1.2,
                    "volatility_20d": 2.2,
                },
                {
                    "code": "300001",
                    "name": "智能过热",
                    "price": 45,
                    "pct_chg": 9.0,
                    "volume_ratio": 4.5,
                    "turnover_rate": 10,
                    "turnover": 1200000000,
                    "sixty_day_pct": 110,
                    "ytd_pct": 180,
                    "amplitude": 12,
                    "ret_10d": 25,
                    "ret_20d": 60,
                    "ma20_gap": 45,
                    "vol_amount_5d": 2.5,
                    "volatility_20d": 8.0,
                },
            ]
        )
        candidates = prepare_candidates(quotes)

        rows, meta = score_position_candidates(candidates, top_n=30)

        self.assertEqual([row["code"] for row in rows], ["688001"])
        self.assertEqual(rows[0]["horizon"], "position")
        self.assertIn("未接入财务", meta["limitation"])
        self.assertHasExplanationFields(rows[0], "position_picks")

    def test_alphalite_factors_detect_momentum_and_breakout(self):
        history = pd.DataFrame(
            {
                "price": [10 + i * 0.2 for i in range(30)],
                "high": [10 + i * 0.2 for i in range(30)],
                "turnover": [10000000 + i * 100000 for i in range(30)],
            }
        )

        factor = compute_alphalite_for_stock("600001", history)

        self.assertGreater(factor["ret_20d"], 0)
        self.assertEqual(factor["breakout_20d"], 1.0)

    def test_topk_dropout_marks_new_and_retained(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = TopKDropoutTracker("{}/state.json".format(tmpdir), keep_k=2, buffer_k=3)
            first = tracker.update(
                "short_term",
                [{"code": "600001", "score": 90}, {"code": "600002", "score": 80}],
            )
            second = tracker.update(
                "short_term",
                [{"code": "600002", "score": 95}, {"code": "600003", "score": 85}],
            )

        self.assertEqual(first["rows"][0]["stability_status"], "new")
        self.assertEqual(second["rows"][0]["code"], "600002")
        self.assertEqual(second["rows"][0]["stability_status"], "retained")
        self.assertIn("600001", second["dropped"])

    def test_backtest_returns_metrics_for_history_pool(self):
        history_by_code = {
            "600001": pd.DataFrame(
                {
                    "price": [10 + i * 0.1 for i in range(60)],
                    "high": [10 + i * 0.1 for i in range(60)],
                    "turnover": [10000000 + i * 50000 for i in range(60)],
                }
            )
        }

        result = run_alphalite_backtest(history_by_code, top_k=1, holding_days=3)

        self.assertTrue(result["ok"])
        self.assertEqual(result["metrics"]["selected_count"], 1)
        self.assertIn("avg_net_return", result["metrics"])

    def test_rolling_backtest_returns_drawdown_metrics(self):
        history_by_code = {
            "600001": pd.DataFrame(
                {
                    "trade_date": ["202401{:02d}".format(i + 1) for i in range(80)],
                    "price": [10 + i * 0.05 for i in range(80)],
                    "high": [10 + i * 0.05 for i in range(80)],
                    "turnover": [10000000 + i * 50000 for i in range(80)],
                }
            ),
            "600002": pd.DataFrame(
                {
                    "trade_date": ["202401{:02d}".format(i + 1) for i in range(80)],
                    "price": [12 + i * 0.03 for i in range(80)],
                    "high": [12 + i * 0.03 for i in range(80)],
                    "turnover": [12000000 + i * 30000 for i in range(80)],
                }
            ),
        }

        result = run_rolling_alphalite_backtest(
            history_by_code,
            top_k=1,
            holding_days=3,
            lookback_days=30,
            rebalance_step=5,
        )

        self.assertTrue(result["ok"])
        self.assertIn("max_drawdown", result["metrics"])
        self.assertGreater(result["metrics"]["period_count"], 0)

    def test_history_cache_round_trip(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            cache = HistoryCache("{}/history.sqlite3".format(tmpdir))
            history = pd.DataFrame(
                {
                    "trade_date": ["20240101", "20240102"],
                    "code": ["600001", "600001"],
                    "open": [10, 11],
                    "high": [11, 12],
                    "low": [9, 10],
                    "price": [10.5, 11.5],
                    "turnover": [10000000, 11000000],
                    "volume": [100000, 110000],
                }
            )

            cache.set("600001", history)
            loaded = cache.get("600001", 10)

        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded.iloc[-1]["price"], 11.5)

    def test_strategy_validation_replaces_same_day_snapshot(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            store = StrategyValidationStore("{}/validation.sqlite3".format(tmpdir))
            first = [
                {"rank": 1, "code": "600001", "name": "旧样本", "price": 10, "score": 80},
                {"rank": 2, "code": "600002", "name": "会被替换", "price": 12, "score": 70},
            ]
            second = [{"rank": 1, "code": "600001", "name": "新样本", "price": 11, "score": 90}]

            store.save_signals("tomorrow_picks", "tomorrow_picks_v2", "2024-01-01T14:30:00", first)
            result = store.save_signals("tomorrow_picks", "tomorrow_picks_v2", "2024-01-01T14:31:00", second)
            rows = store.signals_for_date("2024-01-01", "tomorrow_picks")

        self.assertEqual(result["replaced"], 2)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["code"], "600001")
        self.assertEqual(rows[0]["name"], "新样本")

    def test_strategy_validation_uses_signal_price_returns(self):
        import tempfile

        class FakeProvider:
            def get_history(self, code, days=180):
                return pd.DataFrame(
                    {
                        "trade_date": ["20240101", "20240102", "20240103", "20240104"],
                        "open": [10, 12, 12.5, 13],
                        "high": [10.5, 13, 13.2, 13.6],
                        "low": [9.8, 11.8, 12.0, 12.7],
                        "price": [10, 12.5, 13.0, 13.5],
                    }
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            store = StrategyValidationStore("{}/validation.sqlite3".format(tmpdir))
            store.save_signals(
                "tomorrow_picks",
                "tomorrow_picks_v2",
                "2024-01-01T14:30:00",
                [{"rank": 1, "code": "600001", "name": "样本", "price": 10, "score": 90}],
            )
            update = store.update_outcomes(FakeProvider(), signal_date="2024-01-01", strategy_name="tomorrow_picks")
            rows = store.signals_for_date("2024-01-01", "tomorrow_picks")
            metrics = store.metrics("tomorrow_picks", days=20)

        self.assertEqual(update["updated"], 1)
        self.assertAlmostEqual(rows[0]["signal_next_close_return"], 25.0)
        self.assertAlmostEqual(rows[0]["next_close_return"], 4.1667)
        self.assertEqual(metrics["avg_next_close_return"], 25.0)
        self.assertEqual(metrics["hit_3pct_rate"], 100.0)
        self.assertEqual(metrics["avg_primary_return"], 25.0)
        self.assertAlmostEqual(
            metrics["avg_primary_return_net"],
            25.0 - config.VALIDATION_TRADE_COST_PCT,
        )

    def test_strategy_validation_splits_real_and_replay_samples(self):
        import tempfile

        histories = {
            "600001": _validation_history("2024-01-01", future_days=3, final_price=10.6),
            "600002": _validation_history("2024-01-01", future_days=3, final_price=9.7),
        }

        class FakeProvider:
            def get_history(self, code, days=180):
                return histories[code]

        with tempfile.TemporaryDirectory() as tmpdir:
            store = StrategyValidationStore("{}/validation.sqlite3".format(tmpdir))
            store.save_signals(
                "tomorrow_picks",
                "tomorrow_picks_v2",
                "2024-01-01T14:30:00",
                [{"rank": 1, "code": "600001", "name": "真实样本", "price": 10, "score": 90}],
            )
            store.save_signals(
                "tomorrow_picks",
                "tomorrow_picks_replay_v1",
                "2024-01-01T14:30:00",
                [{"rank": 2, "code": "600002", "name": "回放样本", "price": 10, "score": 80}],
            )
            update = store.update_outcomes(FakeProvider(), strategy_name="tomorrow_picks")
            metrics = store.metrics("tomorrow_picks", days=20)

        self.assertEqual(update["updated"], 2)
        self.assertEqual(metrics["sample_count"], 2)
        self.assertEqual(metrics["outcome_sample_count"], 2)
        self.assertEqual(metrics["real_sample_count"], 1)
        self.assertEqual(metrics["replay_sample_count"], 1)
        self.assertEqual(metrics["primary_horizon_label"], "次日")

    def test_long_horizon_validation_requires_mature_future_days(self):
        import tempfile

        histories = {
            "600001": _validation_history("2024-01-01", future_days=4, final_price=10.4),
            "600002": _validation_history("2024-01-01", future_days=20, final_price=12.0),
        }

        class FakeProvider:
            def get_history(self, code, days=180):
                return histories[code]

        with tempfile.TemporaryDirectory() as tmpdir:
            store = StrategyValidationStore("{}/validation.sqlite3".format(tmpdir))
            store.save_signals(
                "position_picks",
                "position_picks_v1",
                "2024-01-01T14:30:00",
                [
                    {"rank": 1, "code": "600001", "name": "未成熟样本", "price": 10, "score": 90},
                    {"rank": 2, "code": "600002", "name": "成熟样本", "price": 10, "score": 80},
                ],
            )
            update = store.update_outcomes(FakeProvider(), strategy_name="position_picks")
            rows = store.signals_for_date("2024-01-01", "position_picks")
            metrics = store.metrics("position_picks", days=20)

        self.assertEqual(update["updated"], 2)
        self.assertEqual(len(rows), 2)
        self.assertEqual(metrics["primary_holding_days"], 20)
        self.assertEqual(metrics["primary_horizon_label"], "20日")
        self.assertEqual(metrics["outcome_sample_count"], 2)
        self.assertEqual(metrics["sample_count"], 1)
        self.assertEqual(metrics["real_sample_count"], 1)
        self.assertAlmostEqual(metrics["avg_primary_return"], 20.0)
        self.assertAlmostEqual(
            metrics["avg_primary_return_net"],
            20.0 - config.VALIDATION_TRADE_COST_PCT,
        )
        self.assertEqual(metrics["daily"][0]["sample_count"], 1)
        self.assertAlmostEqual(
            metrics["daily"][0]["avg_primary_return_net"],
            20.0 - config.VALIDATION_TRADE_COST_PCT,
        )

    def test_strategy_validation_signal_codes_groups_saved_predictions(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            store = StrategyValidationStore("{}/validation.sqlite3".format(tmpdir))
            store.save_signals(
                "tomorrow_picks",
                "tomorrow_picks_v2",
                "2024-01-01T14:30:00",
                [
                    {"rank": 2, "code": "600001", "name": "样本A", "price": 10, "score": 80},
                    {"rank": 1, "code": "600002", "name": "样本B", "price": 12, "score": 90},
                ],
            )
            store.save_signals(
                "swing_picks",
                "swing_5_10d_v1",
                "2024-01-02T14:30:00",
                [{"rank": 1, "code": "600001", "name": "样本A", "price": 11, "score": 85}],
            )
            rows = store.signal_codes(strategy_name="tomorrow_picks")
            all_rows = store.signal_codes()

        self.assertEqual([row["code"] for row in rows], ["600002", "600001"])
        self.assertEqual(len(all_rows), 2)
        self.assertEqual(all_rows[0]["code"], "600001")
        self.assertEqual(all_rows[0]["signal_count"], 2)

    def test_prefetch_history_endpoint_downloads_then_updates_validation(self):
        import tempfile

        history = pd.DataFrame(
            {
                "trade_date": ["20240101", "20240102", "20240103", "20240104"],
                "open": [10, 12, 12.5, 13],
                "high": [10.5, 13, 13.2, 13.6],
                "low": [9.8, 11.8, 12.0, 12.7],
                "price": [10, 12.5, 13.0, 13.5],
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            validation_path = "{}/validation.sqlite3".format(tmpdir)
            store = StrategyValidationStore(validation_path)
            store.save_signals(
                "tomorrow_picks",
                "tomorrow_picks_v2",
                "2024-01-01T14:30:00",
                [{"rank": 1, "code": "600001", "name": "样本", "price": 10, "score": 90}],
            )
            with patch.object(config, "VALIDATION_DB_PATH", validation_path), patch.object(
                config, "STATE_PATH", "{}/state.json".format(tmpdir)
            ), patch(
                "stock_analyzer.providers.MarketDataProvider.prefetch_history",
                return_value={"requested": 1, "unique_codes": 1, "downloaded": 1, "cached": 0, "failed": 0, "errors": []},
            ), patch(
                "stock_analyzer.providers.MarketDataProvider.get_history",
                return_value=history,
            ):
                app = create_app()
                client = app.test_client()
                response = client.post(
                    "/api/strategy-validation/prefetch-history?strategy=tomorrow_picks&date=2024-01-01&update=1"
                )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["prefetch"]["downloaded"], 1)
        self.assertEqual(payload["outcome"]["updated"], 1)
        self.assertEqual(payload["codes"][0]["code"], "600001")

    def test_backfill_strategy_validation_samples_creates_replay_outcomes(self):
        import tempfile

        dates = pd.date_range("2024-01-01", periods=70, freq="D").strftime("%Y%m%d").tolist()
        histories = {
            "600001": pd.DataFrame(
                {
                    "trade_date": dates,
                    "open": [10 + i * 0.04 for i in range(70)],
                    "high": [10.2 + i * 0.04 for i in range(70)],
                    "low": [9.8 + i * 0.04 for i in range(70)],
                    "price": [10.1 + i * 0.04 for i in range(70)],
                    "turnover": [10000000 + i * 100000 for i in range(70)],
                }
            ),
            "600002": pd.DataFrame(
                {
                    "trade_date": dates,
                    "open": [12 + i * 0.02 for i in range(70)],
                    "high": [12.1 + i * 0.02 for i in range(70)],
                    "low": [11.9 + i * 0.02 for i in range(70)],
                    "price": [12.05 + i * 0.02 for i in range(70)],
                    "turnover": [9000000 + i * 80000 for i in range(70)],
                }
            ),
        }

        class FakeProvider:
            def get_history(self, code, days=180):
                return histories[code]

        with tempfile.TemporaryDirectory() as tmpdir:
            store = StrategyValidationStore("{}/validation.sqlite3".format(tmpdir))
            result = backfill_strategy_validation_samples(
                FakeProvider(),
                store,
                "tomorrow_picks",
                ["600001", "600002"],
                code_names={"600001": "样本A", "600002": "样本B"},
                days=90,
                replay_days=5,
                top_n=2,
                holding_days=3,
            )
            metrics = store.metrics("tomorrow_picks", days=20)
            dates_saved = store.list_signal_dates("tomorrow_picks")
            rows = store.signals_for_date(dates_saved[0]["signal_date"], "tomorrow_picks")

        self.assertTrue(result["ok"])
        self.assertEqual(result["date_count"], 5)
        self.assertEqual(result["saved"], 10)
        self.assertEqual(result["outcome"]["updated"], 10)
        self.assertEqual(metrics["sample_count"], 10)
        self.assertEqual(rows[0]["strategy_version"], "tomorrow_picks_replay_v1")
        self.assertTrue(rows[0]["raw"]["replay"])

    def test_backfill_samples_endpoint_grows_validation_sample_count(self):
        import tempfile

        dates = pd.date_range("2024-01-01", periods=75, freq="D").strftime("%Y%m%d").tolist()
        history = pd.DataFrame(
            {
                "trade_date": dates,
                "open": [10 + i * 0.03 for i in range(75)],
                "high": [10.2 + i * 0.03 for i in range(75)],
                "low": [9.8 + i * 0.03 for i in range(75)],
                "price": [10.05 + i * 0.03 for i in range(75)],
                "turnover": [10000000 + i * 90000 for i in range(75)],
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            validation_path = "{}/validation.sqlite3".format(tmpdir)
            store = StrategyValidationStore(validation_path)
            store.save_signals(
                "tomorrow_picks",
                "tomorrow_picks_v2",
                "2024-01-01T14:30:00",
                [{"rank": 1, "code": "600001", "name": "样本", "price": 10, "score": 90}],
            )
            with patch.object(config, "VALIDATION_DB_PATH", validation_path), patch.object(
                config, "STATE_PATH", "{}/state.json".format(tmpdir)
            ), patch(
                "stock_analyzer.providers.MarketDataProvider.prefetch_history",
                return_value={"requested": 1, "unique_codes": 1, "downloaded": 1, "cached": 0, "failed": 0, "errors": []},
            ), patch(
                "stock_analyzer.providers.MarketDataProvider.get_history",
                return_value=history,
            ):
                app = create_app()
                client = app.test_client()
                response = client.post(
                    "/api/strategy-validation/backfill-samples?strategy=tomorrow_picks&days=120&replay_days=4&top_n=1"
                )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["replay"]["saved"], 4)
        self.assertEqual(payload["replay"]["outcome"]["updated"], 5)
        self.assertGreaterEqual(payload["metrics"]["sample_count"], 4)
        self.assertEqual(payload["replay"]["version"], "tomorrow_picks_replay_v1")

    def test_recommendations_endpoint_returns_market_regime_and_consensus(self):
        import tempfile

        quotes = pd.DataFrame(
            [
                {
                    "code": "600001",
                    "name": "芯片科技",
                    "price": 12,
                    "pct_chg": 3.2,
                    "speed": 1.5,
                    "volume_ratio": 1.8,
                    "turnover_rate": 5,
                    "turnover": 700000000,
                    "industry": "半导体",
                    "sixty_day_pct": 18,
                    "ytd_pct": 32,
                    "amplitude": 5,
                },
                {
                    "code": "600002",
                    "name": "普通样本",
                    "price": 9,
                    "pct_chg": 0.8,
                    "speed": 0.2,
                    "volume_ratio": 1.1,
                    "turnover_rate": 2,
                    "turnover": 120000000,
                    "industry": "银行",
                    "sixty_day_pct": 6,
                    "ytd_pct": 8,
                    "amplitude": 3,
                },
            ]
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(config, "STATE_PATH", "{}/state.json".format(tmpdir)), patch.object(
                config, "VALIDATION_DB_PATH", "{}/validation.sqlite3".format(tmpdir)
            ), patch.object(config, "ENABLE_INLINE_SENTIMENT", False), patch.object(
                config, "ENABLE_MARKET_NEWS", False
            ), patch.object(config, "ENABLE_HISTORY_FACTORS", False), patch.object(
                config, "ENABLE_HOT_RANKS", False
            ), patch.object(
                config, "ENABLE_INDUSTRY_STRENGTH", False
            ), patch.object(
                config, "DEFAULT_TOP_N", 10
            ), patch(
                "stock_analyzer.app.MarketDataProvider.get_realtime_quotes",
                return_value=quotes,
            ):
                app = create_app()
                client = app.test_client()
                response = client.get("/api/recommendations?top_n=10&market=all")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["meta"]["market_regime"]["level"], "risk_on")
        self.assertTrue(payload["meta"]["strategy_consensus"]["rows"])
        consensus = payload["meta"]["strategy_consensus"]["rows"][0]
        self.assertEqual(consensus["code"], "600001")
        self.assertGreaterEqual(consensus["appearances"], 2)
        self.assertIn("avg_quality", consensus)
        self.assertIn("avg_risk", consensus)
        self.assertIn("avg_agent_score", consensus)
        self.assertIn("trading_agents_reference", payload["meta"]["strategy_consensus"])
        self.assertEqual(
            payload["meta"]["strategy_consensus"]["trading_agents_reference"]["repo"],
            "TauricResearch/TradingAgents",
        )
        serenity_refs = payload["meta"]["strategy_consensus"]["serenity_references"]
        self.assertGreaterEqual(len(serenity_refs), 2)
        self.assertIn("Serenity", serenity_refs[0]["repo"])
        self.assertEqual(payload["recommendations"]["short_term"][0]["code"], "600001")
        self.assertIn("consensus_signal", payload["recommendations"]["short_term"][0])
        self.assertIn("serenity_profile", payload["recommendations"]["short_term"][0])
        self.assertIn("agent_committee", payload["recommendations"]["short_term"][0])

    def test_stock_prediction_endpoint_returns_strategy_direction(self):
        import tempfile

        quotes = pd.DataFrame(
            [
                {
                    "code": "600001",
                    "name": "芯片设备",
                    "price": 12,
                    "pct_chg": 3.2,
                    "speed": 1.5,
                    "volume_ratio": 1.8,
                    "turnover_rate": 5,
                    "turnover": 700000000,
                    "industry": "半导体设备",
                    "sixty_day_pct": 18,
                    "ytd_pct": 32,
                    "amplitude": 5,
                },
                {
                    "code": "600002",
                    "name": "普通样本",
                    "price": 9,
                    "pct_chg": 0.8,
                    "speed": 0.2,
                    "volume_ratio": 1.1,
                    "turnover_rate": 2,
                    "turnover": 120000000,
                    "industry": "银行",
                    "sixty_day_pct": 6,
                    "ytd_pct": 8,
                    "amplitude": 3,
                },
            ]
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(config, "STATE_PATH", "{}/state.json".format(tmpdir)), patch.object(
                config, "VALIDATION_DB_PATH", "{}/validation.sqlite3".format(tmpdir)
            ), patch.object(config, "ENABLE_HISTORY_FACTORS", False), patch(
                "stock_analyzer.app.MarketDataProvider.get_realtime_quotes",
                return_value=quotes,
            ):
                app = create_app()
                client = app.test_client()
                response = client.get("/api/stock-prediction/600001")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["code"], "600001")
        self.assertIn(payload["prediction"]["direction"], {"up", "neutral", "down"})
        self.assertIn(payload["horizons"]["short"]["prediction"]["direction"], {"up", "neutral", "down"})
        self.assertIn(payload["horizons"]["long"]["prediction"]["direction"], {"up", "neutral", "down"})
        self.assertGreaterEqual(len(payload["horizons"]["short"]["strategy_hits"]), 1)
        self.assertGreaterEqual(len(payload["horizons"]["long"]["strategy_hits"]), 1)
        self.assertGreaterEqual(len(payload["strategy_hits"]), 1)
        self.assertIn("tomorrow_picks", [row["strategy_name"] for row in payload["strategy_hits"]])
        self.assertIn("long_term", [row["strategy_name"] for row in payload["strategy_hits"]])
        self.assertIn("disclaimer", payload)

    def test_stock_prediction_endpoint_returns_risk_diagnosis_for_filtered_stock(self):
        import tempfile

        quotes = pd.DataFrame(
            [
                {
                    "code": "600003",
                    "name": "低流动样本",
                    "price": 5,
                    "pct_chg": 1.0,
                    "volume_ratio": 0.4,
                    "turnover": 1000,
                    "sixty_day_pct": -12,
                    "ytd_pct": -18,
                    "industry": "银行",
                }
            ]
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(config, "STATE_PATH", "{}/state.json".format(tmpdir)), patch.object(
                config, "VALIDATION_DB_PATH", "{}/validation.sqlite3".format(tmpdir)
            ), patch.object(config, "ENABLE_HISTORY_FACTORS", False), patch(
                "stock_analyzer.app.MarketDataProvider.get_realtime_quotes",
                return_value=quotes,
            ):
                app = create_app()
                client = app.test_client()
                response = client.get("/api/stock-prediction/600003")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["filtered"])
        self.assertEqual(payload["prediction"]["direction"], "down")
        self.assertEqual(payload["horizons"]["short"]["prediction"]["direction"], "down")
        self.assertEqual(payload["horizons"]["long"]["prediction"]["direction"], "down")
        self.assertIn("成交额不足", "；".join(payload["risk_flags"]))
        self.assertEqual(payload["strategy_hits"], [])

    def test_strategy_overview_endpoint_returns_market_regime(self):
        import tempfile

        quotes = pd.DataFrame(
            [
                {
                    "code": "600001",
                    "name": "芯片科技",
                    "price": 12,
                    "pct_chg": 3.2,
                    "volume_ratio": 1.8,
                    "turnover_rate": 5,
                    "turnover": 700000000,
                    "industry": "半导体",
                    "sixty_day_pct": 18,
                    "ytd_pct": 32,
                    "amplitude": 5,
                }
            ]
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(config, "STATE_PATH", "{}/state.json".format(tmpdir)), patch.object(
                config, "VALIDATION_DB_PATH", "{}/validation.sqlite3".format(tmpdir)
            ), patch(
                "stock_analyzer.app.MarketDataProvider.get_realtime_quotes",
                return_value=quotes,
            ):
                app = create_app()
                client = app.test_client()
                response = client.get("/api/strategy-overview?days=20")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["market_regime"]["label"], "偏进攻")
        self.assertEqual(len(payload["strategies"]), 8)
        for name in ("chokepoint_picks", "reversal_picks", "smallcap_value_picks", "breakout_picks"):
            self.assertIn(name, [s["name"] for s in payload["strategies"]])

    def test_eastmoney_normalization_maps_required_quote_fields(self):
        raw = pd.DataFrame(
            [
                {
                    "f2": "12.3",
                    "f3": "4.5",
                    "f4": "0.53",
                    "f5": "1000",
                    "f6": "90000000",
                    "f7": "6.1",
                    "f8": "3.2",
                    "f10": "1.4",
                    "f12": "600001",
                    "f14": "样本股份",
                    "f15": "12.5",
                    "f16": "11.9",
                    "f17": "12.0",
                    "f18": "11.77",
                    "f22": "0.2",
                    "f24": "20",
                    "f25": "12",
                }
            ]
        )

        result = rename_known_columns(_normalize_eastmoney_spot(raw))

        self.assertEqual(result.iloc[0]["code"], "600001")
        self.assertEqual(result.iloc[0]["name"], "样本股份")
        self.assertEqual(result.iloc[0]["price"], 12.3)
        self.assertEqual(result.iloc[0]["turnover"], 90000000)

    def test_provider_prefers_direct_eastmoney_quotes(self):
        provider = MarketDataProvider()

        def fail():
            raise RuntimeError("akshare failed")

        provider._fetch_akshare_quotes = fail
        provider._fetch_eastmoney_quotes = lambda: pd.DataFrame(
            [{"code": "600001", "name": "样本股份", "price": 12, "pct_chg": 3, "turnover": 90000000}]
        )

        quotes = provider.get_realtime_quotes()

        self.assertEqual(str(quotes.iloc[0]["code"]).zfill(6), "600001")
        self.assertEqual(provider.status.quotes_source, "东方财富直连")
        self.assertEqual(provider.status.errors, [])

    def test_provider_fails_fast_when_direct_quotes_fail_by_default(self):
        import tempfile

        provider = MarketDataProvider()

        def fail():
            raise RuntimeError("eastmoney failed")

        provider._fetch_eastmoney_quotes = fail

        original_path = config.QUOTE_SNAPSHOT_PATH
        with tempfile.TemporaryDirectory() as tmpdir:
            config.QUOTE_SNAPSHOT_PATH = "{}/missing.json".format(tmpdir)
            try:
                with self.assertRaises(RuntimeError):
                    provider.get_realtime_quotes()
            finally:
                config.QUOTE_SNAPSHOT_PATH = original_path

        self.assertEqual(provider.status.quotes_source, "unavailable")
        self.assertIn("东方财富直连行情失败", provider.status.errors[0])

    def test_provider_uses_quote_snapshot_when_direct_quotes_fail(self):
        import tempfile

        provider = MarketDataProvider()
        snapshot = pd.DataFrame(
            [{"code": "600001", "name": "样本股份", "price": 12, "pct_chg": 3, "turnover": 90000000}]
        )

        def fail():
            raise RuntimeError("eastmoney failed")

        provider._fetch_eastmoney_quotes = fail
        original_path = config.QUOTE_SNAPSHOT_PATH
        original_min_rows = config.QUOTE_SNAPSHOT_MIN_ROWS
        with tempfile.TemporaryDirectory() as tmpdir:
            config.QUOTE_SNAPSHOT_PATH = "{}/quotes.json".format(tmpdir)
            config.QUOTE_SNAPSHOT_MIN_ROWS = 1
            provider._save_quote_snapshot(snapshot)
            try:
                quotes = provider.get_realtime_quotes()
            finally:
                config.QUOTE_SNAPSHOT_PATH = original_path
                config.QUOTE_SNAPSHOT_MIN_ROWS = original_min_rows

        self.assertEqual(str(quotes.iloc[0]["code"]).zfill(6), "600001")
        self.assertEqual(provider.status.quotes_source, "本地快照")

    def test_provider_falls_back_to_akshare_quotes_when_enabled(self):
        provider = MarketDataProvider()

        def fail():
            raise RuntimeError("eastmoney failed")

        provider._fetch_eastmoney_quotes = fail
        provider._fetch_akshare_quotes = lambda: pd.DataFrame(
            [{"code": "600001", "name": "样本股份", "price": 12, "pct_chg": 3, "turnover": 90000000}]
        )

        original = config.ALLOW_SLOW_QUOTE_FALLBACK
        config.ALLOW_SLOW_QUOTE_FALLBACK = True
        try:
            quotes = provider.get_realtime_quotes()
        finally:
            config.ALLOW_SLOW_QUOTE_FALLBACK = original

        self.assertEqual(quotes.iloc[0]["code"], "600001")
        self.assertEqual(provider.status.quotes_source, "AKShare 东方财富")

    def test_eastmoney_request_uses_proxy_environment_first(self):
        payload = {"data": {"diff": [{"f12": "600001"}]}}
        response = MagicMock()
        response.json.return_value = payload
        session = MagicMock()
        session.__enter__.return_value = session
        session.get.return_value = response

        with patch("stock_analyzer.providers.requests.Session", return_value=session):
            result = _request_eastmoney_page({"pn": "1"})

        self.assertEqual(result, payload)
        self.assertTrue(session.trust_env)
        session.get.assert_called_once()

    def test_eastmoney_request_retries_without_proxy_environment(self):
        payload = {"data": {"diff": [{"f12": "600001"}]}}
        response = MagicMock()
        response.json.return_value = payload
        env_session = MagicMock()
        env_session.__enter__.return_value = env_session
        env_session.get.side_effect = RuntimeError("proxy failed")
        direct_session = MagicMock()
        direct_session.__enter__.return_value = direct_session
        direct_session.get.return_value = response

        with patch(
            "stock_analyzer.providers.requests.Session",
            side_effect=[env_session, direct_session],
        ):
            result = _request_eastmoney_page({"pn": "1"})

        self.assertEqual(result, payload)
        self.assertTrue(env_session.trust_env)
        self.assertFalse(direct_session.trust_env)


def _validation_history(start_date: str, future_days: int, final_price: float) -> pd.DataFrame:
    dates = pd.date_range(start_date, periods=future_days + 1, freq="D").strftime("%Y%m%d").tolist()
    future_prices = [
        10 + (final_price - 10) * (idx + 1) / max(1, future_days)
        for idx in range(future_days)
    ]
    prices = [10] + future_prices
    return pd.DataFrame(
        {
            "trade_date": dates,
            "open": prices,
            "high": [price * 1.01 for price in prices],
            "low": [price * 0.99 for price in prices],
            "price": prices,
        }
    )


if __name__ == "__main__":
    unittest.main()

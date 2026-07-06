import json
import os
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

from stock_analyzer import config
from stock_analyzer.app import create_app
from stock_analyzer.backtest import run_alphalite_backtest, run_rolling_alphalite_backtest
from stock_analyzer.event_risk import attach_event_risk, build_event_risk_map
from stock_analyzer.factors import compute_alphalite_for_stock
from stock_analyzer.factor_ic import compute_factor_ic
from stock_analyzer.fundamentals import attach_fundamental_factors, load_fundamentals
from stock_analyzer.history_cache import HistoryCache
from stock_analyzer.normalization import rename_known_columns
from stock_analyzer.paper_trading import PaperTradingStore
from stock_analyzer.portfolio import build_portfolio
from stock_analyzer.providers import MarketDataProvider, _normalize_eastmoney_spot, _request_eastmoney_page
from stock_analyzer.risk_rules import simulate_exit
from stock_analyzer.risk_blacklist import attach_risk_blacklist, load_risk_blacklist
from stock_analyzer.scoring import (
    build_market_regime,
    build_strategy_consensus,
    candidate_filter_report,
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
from stock_analyzer.snapshot import run_snapshot
from stock_analyzer.strategy_validation import StrategyValidationStore, _primary_return_config
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

    def test_risk_blacklist_hard_filters_json_high_risk(self):
        path = os.path.join("/tmp", "risk_blacklist_test.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "items": {
                        "600001": {
                            "name": "风险样本",
                            "level": "critical",
                            "category": "financial_fraud",
                            "reason": "历史财务造假测试",
                            "hard_exclude": True,
                        }
                    }
                },
                handle,
                ensure_ascii=False,
            )
        quotes = pd.DataFrame(
            [
                {"code": "600001", "name": "风险样本", "price": 10, "pct_chg": 3, "turnover": 100000000},
                {"code": "600002", "name": "正常样本", "price": 10, "pct_chg": 3, "turnover": 100000000},
            ]
        )
        with patch.object(config, "RISK_BLACKLIST_PATH", path), patch.object(config, "RISK_BLACKLIST_CSV_PATH", ""), patch.object(config, "RISK_BLACKLIST_HARD_FILTER", True):
            payload = load_risk_blacklist()
            result = attach_risk_blacklist(prepare_candidates(quotes), payload)

        self.assertEqual(set(result["code"]), {"600002"})
        self.assertEqual(payload["items"]["600001"]["flags"][0]["label"], "历史财务造假测试")

    def test_risk_blacklist_medium_marks_without_filtering(self):
        path = os.path.join("/tmp", "risk_blacklist_test.csv")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("code,name,level,category,reason,hard_exclude\n")
            handle.write("600001,风险样本,medium,negative_history,历史负面测试,false\n")
        quotes = pd.DataFrame(
            [
                {"code": "600001", "name": "风险样本", "price": 10, "pct_chg": 3, "turnover": 100000000},
            ]
        )
        with patch.object(config, "RISK_BLACKLIST_PATH", ""), patch.object(config, "RISK_BLACKLIST_CSV_PATH", path), patch.object(config, "RISK_BLACKLIST_HARD_FILTER", True):
            payload = load_risk_blacklist()
            result = attach_risk_blacklist(prepare_candidates(quotes), payload)

        self.assertEqual(set(result["code"]), {"600001"})
        self.assertEqual(result.iloc[0]["blacklist_risk_level"], "medium")
        self.assertFalse(bool(result.iloc[0]["blacklist_hard_exclude"]))

    def test_risk_blacklist_ignores_entries_without_code(self):
        path = os.path.join("/tmp", "risk_blacklist_missing_code_test.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "items": [
                        {
                            "name": "缺代码样本",
                            "level": "critical",
                            "category": "financial_fraud",
                            "reason": "缺少股票代码",
                        }
                    ]
                },
                handle,
                ensure_ascii=False,
            )

        with patch.object(config, "RISK_BLACKLIST_PATH", path), patch.object(config, "RISK_BLACKLIST_CSV_PATH", ""), patch.object(config, "RISK_BLACKLIST_HARD_FILTER", True):
            payload = load_risk_blacklist()

        self.assertEqual(payload["items"], {})
        self.assertEqual(payload["status"], "empty")

    def test_risk_blacklist_noops_when_code_column_missing(self):
        df = pd.DataFrame([{"name": "无代码样本", "price": 10}])

        result = attach_risk_blacklist(df, {"status": "ok", "items": {}})

        self.assertEqual(result.to_dict("records"), df.to_dict("records"))
        self.assertIsNot(result, df)

    def test_candidate_filter_report_matches_prepare_candidates(self):
        quotes = pd.DataFrame(
            [
                {"code": "600001", "name": "正常样本", "price": 10, "pct_chg": 3, "turnover": 100000000},
                {"code": "430001", "name": "北交样本", "price": 10, "pct_chg": 3, "turnover": 100000000},
                {"code": "600002", "name": "ST样本", "price": 10, "pct_chg": 3, "turnover": 100000000},
                {"code": "600003", "name": "低流动", "price": 10, "pct_chg": 3, "turnover": 1000},
                {"code": "600004", "name": "涨幅过高", "price": 10, "pct_chg": 13, "turnover": 100000000},
                {"code": "600005", "name": "接近涨停", "price": 10, "pct_chg": 9.5, "turnover": 100000000},
            ]
        )

        prepared = prepare_candidates(quotes)
        report = candidate_filter_report(quotes)

        self.assertEqual(report["raw_count"], len(quotes))
        self.assertEqual(report["passed_count"], len(prepared))
        self.assertEqual(report["rejected_count"], len(quotes) - len(prepared))
        self.assertEqual(set(prepared["code"]), {"600001"})
        reason_keys = {item["key"] for item in report["reasons"]}
        self.assertIn("unsupported_code", reason_keys)
        self.assertIn("special_treatment", reason_keys)
        self.assertIn("min_turnover", reason_keys)
        self.assertIn("max_gain", reason_keys)
        self.assertIn("buyable_gain", reason_keys)

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

    def test_market_regime_breadth_can_use_full_quote_source(self):
        quotes = pd.DataFrame(
            [
                {"code": "600001", "name": "强势A", "price": 10, "pct_chg": 5, "turnover": 100000000, "amplitude": 4},
                {"code": "600002", "name": "强势B", "price": 10, "pct_chg": 4, "turnover": 100000000, "amplitude": 4},
                {"code": "600003", "name": "弱势C", "price": 10, "pct_chg": -5, "turnover": 1000000, "amplitude": 5},
                {"code": "600004", "name": "弱势D", "price": 10, "pct_chg": -4, "turnover": 1000000, "amplitude": 5},
            ]
        )
        candidates = prepare_candidates(quotes)
        regime = build_market_regime(candidates, breadth_source=quotes)

        self.assertEqual(len(candidates), 2)
        self.assertEqual(regime["breadth_pct"], 50.0)
        self.assertLessEqual(regime["median_pct_chg"], 0.5)

    def test_market_regime_reports_history_breadth_when_factors_exist(self):
        quotes = pd.DataFrame(
            [
                {"code": "600001", "name": "强势A", "price": 10, "pct_chg": 2, "turnover": 100000000, "amplitude": 4, "ma20_gap": 3, "alphalite_factor_ready": 1},
                {"code": "600002", "name": "弱势B", "price": 10, "pct_chg": -1, "turnover": 100000000, "amplitude": 4, "ma20_gap": -2, "alphalite_factor_ready": 1},
            ]
        )

        regime = build_market_regime(prepare_candidates(quotes))

        self.assertEqual(regime["history_breadth20_pct"], 50.0)
        self.assertEqual(regime["history_factor_coverage_pct"], 100.0)

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
        # A4：高分但历史因子覆盖不足 → 强制降级到 watch，且带 note
        gated = _verdict_tier(85, 30, 0.2)
        self.assertEqual(gated["tier"], "watch")
        self.assertEqual(gated["note"], "历史因子覆盖不足，评级降级")

    def test_data_coverage_uses_factor_metadata_not_nonzero_values(self):
        from stock_analyzer.scoring import _data_coverage

        row = pd.Series({"alphalite_coverage": 0.67, "ret_20d": 0.0, "breakout_20d": 0.0})
        self.assertAlmostEqual(_data_coverage(row), 0.67)

    def test_consensus_stretch_rewards_agreement(self):
        from stock_analyzer.scoring import _consensus_stretch

        # 同一基础分，高一致性应被拉得比低一致性更高（>50 区间）。
        high = _consensus_stretch(80, 1.0)
        low = _consensus_stretch(80, 0.0)
        self.assertGreater(high, low)
        self.assertGreater(high, 80)
        self.assertLess(low, 80)

    def test_overheat_damp_suppresses_extended_names(self):
        from stock_analyzer.scoring import _apply_overheat_damp, _overheat_damp_multiplier

        calm = pd.Series({"sixty_day_pct": 5, "ytd_pct": 10, "amplitude": 4})
        extended = pd.Series({"sixty_day_pct": 130, "ytd_pct": 160, "amplitude": 15})
        # 过热票的 final 被乘法压低，明显低于温和票。
        self.assertLess(_apply_overheat_damp(80, extended), _apply_overheat_damp(80, calm))
        self.assertLessEqual(_apply_overheat_damp(80, extended), 80)
        self.assertLess(_overheat_damp_multiplier(extended), _overheat_damp_multiplier(calm))

    def test_overheat_is_not_repeated_in_tomorrow_risk_penalty(self):
        from stock_analyzer.scoring import _tomorrow_risk_penalty_parts, _overheat_damp_multiplier

        extended = pd.Series(
            {
                "pct_chg": 2.0,
                "market": "main",
                "amplitude": 4.0,
                "turnover_rate": 5.0,
                "volume_ratio": 1.5,
                "sixty_day_pct": 120.0,
                "ytd_pct": 180.0,
            }
        )

        parts = _tomorrow_risk_penalty_parts(extended)

        self.assertEqual(parts, {})
        self.assertLess(_overheat_damp_multiplier(extended), 1.0)

    def test_tomorrow_rows_expose_combiner_diagnostics(self):
        quotes = pd.DataFrame(
            [
                {"code": "600001", "name": "样本A", "price": 10, "pct_chg": 2, "turnover": 8e8,
                 "turnover_rate": 5, "volume_ratio": 1.8, "speed": 0.6, "sixty_day_pct": 10,
                 "ytd_pct": 20, "amplitude": 4},
                {"code": "600002", "name": "样本B", "price": 11, "pct_chg": 1, "turnover": 7e8,
                 "turnover_rate": 4, "volume_ratio": 1.2, "speed": 0.2, "sixty_day_pct": 4,
                 "ytd_pct": 8, "amplitude": 3},
            ]
        )
        rows, _ = score_tomorrow_candidates(prepare_candidates(quotes), top_n=2)

        self.assertTrue(rows)
        row = rows[0]
        self.assertIn("base_score", row)
        self.assertIn("raw_score", row)
        self.assertIn("overheat_damp", row)
        self.assertIn("tail_setup_score", row)
        self.assertIn("risk_penalty_parts", row)

    def test_tomorrow_rejects_weak_tail_close(self):
        quotes = pd.DataFrame(
            [
                {"code": "600001", "name": "尾盘强", "price": 10.8, "open": 10.2, "high": 11.0, "low": 10.0,
                 "pct_chg": 4.0, "turnover": 8e8, "turnover_rate": 5, "volume_ratio": 2.0,
                 "speed": 0.4, "sixty_day_pct": 12, "ytd_pct": 16, "amplitude": 6},
                {"code": "600002", "name": "尾盘弱", "price": 10.2, "open": 10.6, "high": 11.0, "low": 10.0,
                 "pct_chg": 3.0, "turnover": 8e8, "turnover_rate": 5, "volume_ratio": 2.0,
                 "speed": -1.5, "sixty_day_pct": 12, "ytd_pct": 16, "amplitude": 6},
            ]
        )

        rows, _ = score_tomorrow_candidates(prepare_candidates(quotes), top_n=10)

        self.assertEqual([row["code"] for row in rows], ["600001"])

    def test_tomorrow_risk_off_keeps_requested_display_count(self):
        quotes = pd.DataFrame(
            [
                {
                    "code": "600{:03d}".format(index),
                    "name": "样本{}".format(index),
                    "price": 10 + index * 0.1,
                    "pct_chg": 2.0 + (index % 3) * 0.2,
                    "turnover": 500000000 + index * 10000000,
                    "turnover_rate": 4 + index % 5,
                    "volume_ratio": 1.5 + (index % 4) * 0.2,
                    "speed": 0.4,
                    "sixty_day_pct": 12 + index,
                    "ytd_pct": 16 + index,
                    "amplitude": 4,
                }
                for index in range(40)
            ]
        )

        rows, meta = score_tomorrow_candidates(
            prepare_candidates(quotes),
            top_n=36,
            market_regime={"level": "risk_off", "label": "偏防守", "score": 38},
        )

        self.assertEqual(len(rows), 36)
        self.assertEqual(meta["display_limit"], 36)
        self.assertIn("补足", meta["gate_reason"])
        self.assertIn(rows[0]["tier"], {"primary_watch", "backup_pool"})

    def test_tomorrow_risk_off_falls_back_to_backup_watch_when_no_strict_match(self):
        quotes = pd.DataFrame(
            [
                {
                    "code": "600{:03d}".format(index),
                    "name": "低分样本{}".format(index),
                    "price": 10,
                    "pct_chg": 1.0,
                    "turnover": 100000000,
                    "turnover_rate": 2,
                    "volume_ratio": 1.0,
                    "speed": 0.0,
                    "sixty_day_pct": 2,
                    "ytd_pct": 3,
                    "amplitude": 3,
                }
                for index in range(12)
            ]
        )

        with patch("stock_analyzer.scoring._tomorrow_display_gate", return_value=(36, 101.0, "测试严格门控")):
            rows, meta = score_tomorrow_candidates(
                prepare_candidates(quotes),
                top_n=36,
                market_regime={"level": "risk_off", "label": "偏防守", "score": 38},
            )

        self.assertTrue(rows)
        self.assertEqual(len(rows), 12)
        self.assertEqual(meta["primary_watch_count"], 0)
        self.assertEqual(rows[0]["tier"], "backup_pool")
        self.assertIn("备选观察", meta["gate_reason"])

    def test_tomorrow_uses_backup_pool_when_strict_filter_rejects_everything(self):
        quotes = pd.DataFrame(
            [
                {
                    "code": "600{:03d}".format(index),
                    "name": "弱势备选{}".format(index),
                    "price": 10,
                    "pct_chg": -0.8,
                    "turnover": 180000000 + index * 1000000,
                    "turnover_rate": 2,
                    "volume_ratio": 1.0,
                    "speed": -0.2,
                    "sixty_day_pct": 4,
                    "ytd_pct": 6,
                    "amplitude": 3,
                }
                for index in range(12)
            ]
        )

        rows, meta = score_tomorrow_candidates(
            prepare_candidates(quotes),
            top_n=36,
            market_regime={"level": "risk_off", "label": "偏防守", "score": 25},
        )

        self.assertTrue(rows)
        self.assertLessEqual(len(rows), 12)
        self.assertEqual(meta["primary_watch_count"], 0)
        self.assertEqual(rows[0]["tier"], "backup_pool")
        self.assertIn("严格筛选为空", meta["gate_reason"])

    def test_tomorrow_weak_history_breadth_disables_primary_watch(self):
        quotes = pd.DataFrame(
            [
                {
                    "code": "600{:03d}".format(index),
                    "name": "弱宽度样本{}".format(index),
                    "price": 10 + index * 0.1,
                    "pct_chg": 2.5,
                    "turnover": 500000000 + index * 10000000,
                    "turnover_rate": 4,
                    "volume_ratio": 1.8,
                    "speed": 0.4,
                    "sixty_day_pct": 12,
                    "ytd_pct": 16,
                    "amplitude": 4,
                    "ret_5d": 2,
                    "ret_10d": 4,
                    "ret_20d": 6,
                    "ma20_gap": -2 if index < 8 else 2,
                    "vol_amount_5d": 1.5,
                    "volatility_20d": 2,
                    "alphalite_factor_ready": 1,
                }
                for index in range(12)
            ]
        )

        rows, meta = score_tomorrow_candidates(
            prepare_candidates(quotes),
            top_n=12,
            market_regime={"level": "risk_on", "label": "偏进攻", "score": 75},
        )

        self.assertTrue(rows)
        self.assertEqual(meta["history_breadth20_pct"], 33.33)
        self.assertEqual(meta["primary_watch_count"], 0)
        self.assertEqual(rows[0]["tier"], "backup_pool")
        self.assertIn("低于45%", meta["gate_reason"])

    def test_tomorrow_overheated_rows_stay_backup_only(self):
        quotes = pd.DataFrame(
            [
                {
                    "code": "300274",
                    "name": "过热样本",
                    "price": 10,
                    "pct_chg": 4.0,
                    "turnover": 1200000000,
                    "turnover_rate": 8,
                    "volume_ratio": 2.0,
                    "speed": 0.4,
                    "sixty_day_pct": 130,
                    "ytd_pct": 180,
                    "amplitude": 4,
                    "industry": "电源设备",
                    "ret_5d": 8,
                    "ret_10d": 16,
                    "ret_20d": 28,
                    "ma20_gap": 12,
                    "vol_amount_5d": 2.0,
                    "volatility_20d": 3,
                    "alphalite_factor_ready": 1,
                },
                {
                    "code": "600001",
                    "name": "强质量样本",
                    "price": 11,
                    "pct_chg": 4.8,
                    "turnover": 2000000000,
                    "turnover_rate": 9,
                    "volume_ratio": 2.4,
                    "speed": 0.8,
                    "sixty_day_pct": 32,
                    "ytd_pct": 45,
                    "amplitude": 4,
                    "industry": "通用设备",
                    "ret_5d": 9,
                    "ret_10d": 17,
                    "ret_20d": 24,
                    "ma20_gap": 10,
                    "vol_amount_5d": 2.2,
                    "volatility_20d": 2,
                    "alphalite_factor_ready": 1,
                },
            ]
        )

        rows, meta = score_tomorrow_candidates(
            prepare_candidates(quotes),
            top_n=36,
            market_regime={"level": "risk_on", "label": "偏进攻", "score": 75},
        )

        overheated = next(row for row in rows if row["code"] == "300274")
        self.assertEqual(overheated["tier"], "backup_pool")
        self.assertIn("过热抑制过强仅备选", overheated["reasons"])
        self.assertEqual(meta["primary_watch_count"], 1)
        self.assertGreaterEqual(meta["primary_ineligible_count"], 1)

    def test_tomorrow_primary_watch_caps_same_theme(self):
        quotes = pd.DataFrame(
            [
                {
                    "code": "600{:03d}".format(index),
                    "name": "半导体样本{}".format(index),
                    "price": 10 + index * 0.1,
                    "pct_chg": 3.0 + index * 0.1,
                    "turnover": 900000000 + index * 10000000,
                    "turnover_rate": 5 + index,
                    "volume_ratio": 1.8,
                    "speed": 0.3,
                    "sixty_day_pct": 18 + index,
                    "ytd_pct": 24 + index,
                    "amplitude": 4,
                    "industry": "半导体",
                    "ret_5d": 4,
                    "ret_10d": 7,
                    "ret_20d": 10,
                    "ma20_gap": 4,
                    "vol_amount_5d": 1.4,
                    "volatility_20d": 2,
                    "alphalite_factor_ready": 1,
                }
                for index in range(4)
            ]
        )

        rows, meta = score_tomorrow_candidates(
            prepare_candidates(quotes),
            top_n=4,
            market_regime={"level": "risk_on", "label": "偏进攻", "score": 75},
        )

        primary_rows = [row for row in rows if row["tier"] == "primary_watch"]
        self.assertLessEqual(len(primary_rows), config.TOMORROW_MAX_PRIMARY_PER_THEME)
        self.assertGreaterEqual(meta["theme_limited_count"], 1)
        self.assertTrue(any("同主题重点观察已达上限" in row["reasons"] for row in rows))

    def test_tomorrow_low_score_fill_remains_backup_pool(self):
        quotes = pd.DataFrame(
            [
                {
                    "code": "600001",
                    "name": "低分补足",
                    "price": 10,
                    "pct_chg": 1.0,
                    "turnover": 120000000,
                    "turnover_rate": 2,
                    "volume_ratio": 1.0,
                    "speed": 0.0,
                    "sixty_day_pct": 2,
                    "ytd_pct": 3,
                    "amplitude": 3,
                    "industry": "低分行业",
                }
            ]
        )

        with patch("stock_analyzer.scoring._tomorrow_display_gate", return_value=(36, 101.0, "测试严格门控")):
            rows, meta = score_tomorrow_candidates(
                prepare_candidates(quotes),
                top_n=36,
                market_regime={"level": "risk_on", "label": "偏进攻", "score": 75},
            )

        self.assertTrue(rows)
        self.assertEqual(rows[0]["tier"], "backup_pool")
        self.assertIn("未达重点分数线", rows[0]["reasons"])
        self.assertEqual(meta["primary_watch_count"], 0)

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

    def test_reversal_uses_oversold_calm_composite(self):
        quotes = pd.DataFrame(
            [
                {"code": "600001", "name": "超跌低波", "price": 10, "pct_chg": 0.5, "turnover": 8e8,
                 "turnover_rate": 3, "volume_ratio": 1.2, "sixty_day_pct": -10, "ytd_pct": -8,
                 "amplitude": 3, "ret_20d": -12, "volatility_20d": 2},
                {"code": "600002", "name": "普通样本", "price": 11, "pct_chg": 0.4, "turnover": 6e8,
                 "turnover_rate": 8, "volume_ratio": 1.1, "sixty_day_pct": 20, "ytd_pct": 30,
                 "amplitude": 8, "ret_20d": 8, "volatility_20d": 8},
            ]
        )

        rows, meta = score_reversal_candidates(prepare_candidates(quotes), top_n=2)

        self.assertTrue(rows)
        self.assertIn("oversold_calm_score", rows[0])
        self.assertIn("factor_correlation", meta)
        self.assertIn("reversal_lowvol", meta["factor_correlation"])

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

        self.assertGreater(rel["real_good_replay_bad"], rel["real_bad_replay_good"])
        self.assertLess(rel["real_good_replay_bad"], 1.0)
        self.assertLess(rel["real_bad_replay_good"], 1.0)

    def test_strategy_reliability_can_zero_decayed_real_strategy(self):
        from stock_analyzer.scoring import _strategy_reliability

        rel = _strategy_reliability(
            {
                "decayed": {
                    "sample_count": 30,
                    "real_sample_count": 25,
                    "replay_sample_count": 0,
                    "real_win_rate_primary_net": 35,
                    "real_avg_primary_return_net": -0.4,
                }
            }
        )

        self.assertEqual(rel["decayed"], 0.0)

    def test_strategy_status_does_not_fallback_when_real_metric_is_zero(self):
        from stock_analyzer.strategy_health import strategy_status

        status = strategy_status(
            {
                "sample_count": 80,
                "real_sample_count": 25,
                "real_win_rate_primary_net": 0.0,
                "real_avg_primary_return_net": 0.0,
                "win_rate_primary_net": 80.0,
                "avg_primary_return_net": 2.0,
            }
        )

        self.assertEqual(status["state"], "retired")

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
        self.assertIn("execution_score", row)
        self.assertIsInstance(row["execution_score"], (int, float))
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

    def test_chokepoint_candidates_include_glass_substrate_segment(self):
        quotes = pd.DataFrame(
            [
                {"code": "603773", "name": "玻璃基板样本", "price": 25, "pct_chg": 2.0, "turnover": 8e8,
                 "turnover_rate": 5, "volume_ratio": 1.6, "speed": 0.8, "sixty_day_pct": 12, "ytd_pct": 20,
                 "amplitude": 4, "industry": "TGV玻璃通孔"},
            ]
        )
        candidates = prepare_candidates(quotes)
        rows, meta = score_chokepoint_candidates(candidates, top_n=10)

        self.assertTrue(rows)
        self.assertEqual(rows[0]["chain_segment"], "玻璃基板/TGV")
        self.assertIn("玻璃基板/TGV", [node["segment"] for node in meta["chain"]])

    def test_chokepoint_candidates_include_satellite_internet_segment(self):
        quotes = pd.DataFrame(
            [
                {"code": "600118", "name": "中国星网样本", "price": 28, "pct_chg": 1.2, "turnover": 8e8,
                 "turnover_rate": 4, "volume_ratio": 1.4, "speed": 0.4, "sixty_day_pct": 10, "ytd_pct": 18,
                 "amplitude": 4, "industry": "卫星互联网低轨星座相控阵终端"},
            ]
        )
        candidates = prepare_candidates(quotes)
        rows, meta = score_chokepoint_candidates(candidates, top_n=10)

        self.assertTrue(rows)
        self.assertEqual(rows[0]["chain_segment"], "卫星互联网/低轨星座")
        self.assertIn("卫星互联网/低轨星座", [node["segment"] for node in meta["chain"]])

    def test_chokepoint_industry_leaders_cover_expanded_segments(self):
        from stock_analyzer.scoring import CHOKEPOINT_INDUSTRY_LEADERS

        segments = set(CHOKEPOINT_INDUSTRY_LEADERS)
        self.assertGreaterEqual(len(segments), 15)
        for segment in (
            "先进光刻/精密光学",
            "AI算力液冷/电源",
            "工业母机/高端数控",
            "高端轴承/丝杠导轨",
            "机器人核心零部件",
            "SiC/GaN功率半导体",
            "工业软件/CAE",
            "高端膜材料/催化剂",
            "基础软件/信创",
            "玻璃基板/TGV",
            "卫星互联网/低轨星座",
        ):
            self.assertIn(segment, segments)
            self.assertTrue(CHOKEPOINT_INDUSTRY_LEADERS[segment])

    def test_chokepoint_industry_map_contains_glass_substrate_leaders(self):
        import tempfile

        quotes = pd.DataFrame(
            [
                {
                    "code": "603773",
                    "name": "沃格光电",
                    "price": 25,
                    "pct_chg": 1.8,
                    "turnover": 800000000,
                    "turnover_rate": 5,
                    "volume_ratio": 1.6,
                    "sixty_day_pct": 12,
                    "ytd_pct": 20,
                    "amplitude": 4,
                    "industry": "玻璃基板",
                },
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
                response = client.get("/api/chokepoint-picks?top_n=10")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        industry_map = payload["meta"]["industry_map"]
        glass = next(node for node in industry_map if node["segment"] == "玻璃基板/TGV")
        self.assertTrue(any(item["code"] == "603773" for item in glass["leaders"]))
        self.assertIn("玻璃基板/TGV", [row["chain_segment"] for row in payload["data"]])

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
        self.assertEqual(meta["analysis_window"], "15:00")
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

    def test_breakout_uses_realtime_fallback_when_history_factors_missing(self):
        quotes = pd.DataFrame(
            [
                {"code": "600001", "name": "实时强势", "price": 20, "pct_chg": 3.2, "turnover": 9e8,
                 "turnover_rate": 7, "volume_ratio": 2.0, "speed": 0.8, "sixty_day_pct": 18, "ytd_pct": 28,
                 "amplitude": 5, "industry": "电子"},
                {"code": "600002", "name": "弱势无量", "price": 10, "pct_chg": 0.2, "turnover": 6e8,
                 "turnover_rate": 3, "volume_ratio": 1.0, "speed": 0.0, "sixty_day_pct": 2, "ytd_pct": 3,
                 "amplitude": 2, "industry": "电子"},
            ]
        )
        candidates = prepare_candidates(quotes)
        rows, meta = score_breakout_candidates(candidates, top_n=10)

        self.assertTrue(rows)
        self.assertEqual(rows[0]["code"], "600001")
        self.assertTrue(rows[0]["breakout_fallback"])
        self.assertFalse(meta["history_signal_available"])
        self.assertIn("兜底", meta["note"])
        json.dumps({"data": rows, "meta": meta}, ensure_ascii=False)

    def test_breakout_uses_realtime_fallback_when_history_partially_covered(self):
        quotes = pd.DataFrame(
            [
                {"code": "600001", "name": "历史普通", "price": 20, "pct_chg": 0.8, "turnover": 8e8,
                 "turnover_rate": 3, "volume_ratio": 1.0, "speed": 0.1, "sixty_day_pct": 4,
                 "ytd_pct": 8, "amplitude": 3, "ma_bull_aligned": 0, "breakout_20d": 0,
                 "vol_ma5_ratio": 1.0, "ma20_gap": -2, "alphalite_factor_ready": 1},
                {"code": "600002", "name": "实时放量", "price": 18, "pct_chg": 3.5, "turnover": 9e8,
                 "turnover_rate": 7, "volume_ratio": 2.1, "speed": 0.8, "sixty_day_pct": 18,
                 "ytd_pct": 25, "amplitude": 5},
            ]
        )
        rows, meta = score_breakout_candidates(prepare_candidates(quotes), top_n=10)

        self.assertTrue(rows)
        self.assertEqual(rows[0]["code"], "600002")
        self.assertTrue(rows[0]["breakout_fallback"])
        self.assertEqual(meta["fallback_count"], 1)

    def test_alphalite_attach_uses_cache_only_by_default_to_avoid_request_blocking(self):
        from stock_analyzer.app import TimedCache, _attach_alphalite_factors

        quotes = pd.DataFrame(
            [
                {"code": "600001", "name": "样本", "price": 20, "pct_chg": 3.0, "turnover": 9e8,
                 "turnover_rate": 7, "volume_ratio": 2.0, "sixty_day_pct": 18, "amplitude": 5},
            ]
        )

        class SlowProvider:
            def get_cached_history(self, code, days=90):
                return pd.DataFrame()

            def get_history(self, code, days=90):
                raise AssertionError("request path should not fetch remote history by default")

        with patch.object(config, "ENABLE_HISTORY_FACTORS", True), patch.object(
            config, "HISTORY_FACTORS_FETCH_ON_REQUEST", False
        ):
            enriched = _attach_alphalite_factors(SlowProvider(), TimedCache(1), prepare_candidates(quotes))

        self.assertIn("alphalite_factor_ready", enriched.columns)
        self.assertEqual(enriched.iloc[0]["alphalite_factor_ready"], 0.0)

    def test_regime_adaptive_weights_boost_breakout_in_risk_on(self):
        quotes = pd.DataFrame(
            [
                {"code": "600001", "name": "突破样本", "price": 20, "pct_chg": 3.0, "turnover": 9e8,
                 "turnover_rate": 7, "volume_ratio": 2.2, "speed": 1.5, "sixty_day_pct": 22, "ytd_pct": 28,
                 "amplitude": 5, "ma_bull_aligned": 1, "breakout_20d": 1, "vol_ma5_ratio": 2.1, "ma20_gap": 9,
                 "industry": "电子"},
            ]
        )
        candidates = prepare_candidates(quotes)

        risk_on_rows, _ = score_breakout_candidates(
            candidates,
            top_n=10,
            market_regime={"level": "risk_on", "label": "偏进攻"},
        )
        risk_off_rows, _ = score_breakout_candidates(
            candidates,
            top_n=10,
            market_regime={"level": "risk_off", "label": "偏防守"},
        )

        self.assertTrue(risk_on_rows)
        self.assertGreater(risk_on_rows[0]["score"], risk_off_rows[0]["score"])
        self.assertIn("regime_weight_profile", risk_on_rows[0])
        self.assertGreater(
            risk_on_rows[0]["regime_weight_profile"]["breakout"],
            risk_off_rows[0]["regime_weight_profile"]["breakout"],
        )

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

    def test_topk_dropout_keeps_today_recommendation_count(self):
        import tempfile

        ranked_rows = [{"code": "600{:03d}".format(index), "score": 100 - index} for index in range(35)]
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = TopKDropoutTracker("{}/state.json".format(tmpdir), keep_k=30, buffer_k=50)
            result = tracker.update("short_term", ranked_rows)

        self.assertEqual(len(result["rows"]), 30)
        self.assertEqual(result["rows"][0]["code"], "600000")
        self.assertEqual(result["rows"][-1]["code"], "600029")

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

    def test_simulate_exit_handles_stop_take_profit_and_trailing(self):
        stop = simulate_exit(
            pd.DataFrame([{"trade_date": "20240102", "high": 10.2, "low": 9.4, "price": 9.8}]),
            entry_price=10,
            holding_days=3,
        )
        take = simulate_exit(
            pd.DataFrame([{"trade_date": "20240102", "high": 10.9, "low": 10.1, "price": 10.5}]),
            entry_price=10,
            holding_days=3,
        )
        trailing = simulate_exit(
            pd.DataFrame(
                [
                    {"trade_date": "20240102", "high": 10.7, "low": 10.2, "price": 10.6},
                    {"trade_date": "20240103", "high": 10.8, "low": 10.3, "price": 10.4},
                ]
            ),
            entry_price=10,
            holding_days=3,
            policy={"stop_loss_pct": 0, "take_profit_pct": 0, "trailing_stop_pct": 4},
        )

        self.assertEqual(stop["exit_reason"], "stop_loss")
        self.assertAlmostEqual(stop["exit_return"], -5.0)
        self.assertEqual(take["exit_reason"], "take_profit")
        self.assertAlmostEqual(take["exit_return"], 8.0)
        self.assertEqual(trailing["exit_reason"], "trailing_stop")
        self.assertGreater(trailing["exit_return"], 0)

    def test_simulate_exit_delays_stop_on_sealed_limit_down(self):
        result = simulate_exit(
            pd.DataFrame(
                [
                    {"trade_date": "20240102", "prev_close": 10.0, "open": 9.0, "high": 9.05, "low": 9.0, "price": 9.0},
                    {"trade_date": "20240103", "prev_close": 9.0, "open": 8.8, "high": 9.0, "low": 8.6, "price": 8.9},
                ]
            ),
            entry_price=10,
            holding_days=1,
            policy={"limit_down_pct": 10},
        )

        self.assertEqual(result["exit_reason"], "stop_loss_limit_down_delayed")
        self.assertEqual(result["exit_days"], 2)
        self.assertAlmostEqual(result["exit_price"], 8.8)

    def test_backtest_uses_exit_rule_before_fixed_holding_period(self):
        prices = [10 + i * 0.1 for i in range(60)]
        lows = [price * 0.99 for price in prices]
        lows[57] = prices[56] * 0.94
        history_by_code = {
            "600001": pd.DataFrame(
                {
                    "price": prices,
                    "high": [price * 1.02 for price in prices],
                    "low": lows,
                    "turnover": [10000000 + i * 50000 for i in range(60)],
                }
            )
        }

        result = run_alphalite_backtest(history_by_code, top_k=1, holding_days=3)

        self.assertTrue(result["ok"])
        selected = result["selected"][0]
        self.assertEqual(selected["exit_reason"], "stop_loss")
        self.assertAlmostEqual(selected["gross_return"], -5.0)
        self.assertGreater(selected["fixed_gross_return"], 0)

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

    def test_strategy_validation_reports_pending_outcomes(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            store = StrategyValidationStore("{}/validation.sqlite3".format(tmpdir))
            rows = [
                {
                    "rank": 1,
                    "code": "600001",
                    "name": "待回填",
                    "market": "main",
                    "price": 10,
                    "pct_chg": 2,
                    "turnover": 200000000,
                    "volume_ratio": 1.5,
                    "turnover_rate": 3,
                    "sixty_day_pct": 8,
                    "ytd_pct": 10,
                    "score": 70,
                    "tier": "primary_watch",
                    "reasons": ["测试"],
                }
            ]

            store.save_signals("tomorrow_picks", "tomorrow_picks_v4", "2024-01-01T14:30:00", rows)
            metrics = store.metrics("tomorrow_picks", days=20)

        self.assertEqual(metrics["signal_sample_count"], 1)
        self.assertEqual(metrics["pending_outcome_count"], 1)
        self.assertEqual(metrics["outcome_coverage_pct"], 0.0)

    def test_build_portfolio_respects_position_single_and_theme_caps(self):
        rows = [
            {
                "rank": idx + 1,
                "code": "60000{}".format(idx),
                "name": "样本{}".format(idx),
                "theme": theme,
                "score": 90 - idx,
                "serenity_profile": {"confidence_score": 80 - idx, "risk_score": 40 + idx},
            }
            for idx, theme in enumerate(["半导体", "半导体", "算力", "算力", "军工", "医药"])
        ]

        result = build_portfolio(rows, max_positions=5, single_cap=0.3, theme_cap=0.5)
        weights = [row["suggested_weight"] for row in result["rows"]]

        self.assertEqual(len(result["rows"]), 5)
        self.assertAlmostEqual(sum(weights), 100.0, places=1)
        self.assertLessEqual(max(weights), 30.01)
        self.assertTrue(all(value <= 50.01 for value in result["exposure"].values()))
        self.assertTrue(result["summary"]["constraints_feasible"])

    def test_event_risk_can_hard_filter_and_tag_candidates(self):
        from datetime import datetime, timedelta

        future_date = (datetime.now() + timedelta(days=5)).strftime("%Y%m%d")
        risk_map = build_event_risk_map(
            unlocks=[{"code": "600001", "unlock_ratio": 12, "date": future_date}],
            pledges=[{"code": "600001", "pledge_ratio": 60}],
        )
        candidates = pd.DataFrame(
            [
                {"code": "600001", "name": "风险样本", "price": 10, "pct_chg": 1, "turnover": 100000000},
                {"code": "600002", "name": "普通样本", "price": 10, "pct_chg": 1, "turnover": 100000000},
            ]
        )

        with patch.object(config, "EVENT_RISK_HARD_FILTER", True):
            filtered = attach_event_risk(prepare_candidates(candidates), {"status": "ok", "items": risk_map})
        tagged = attach_event_risk(prepare_candidates(candidates), {"status": "ok", "items": risk_map})

        self.assertEqual(set(filtered["code"]), {"600002"})
        risk_row = tagged[tagged["code"] == "600001"].iloc[0]
        self.assertGreater(risk_row["event_risk_penalty"], 0)
        self.assertTrue(risk_row["event_risk_flags"])

    def test_event_risk_raises_profile_risk_and_adds_reasons(self):
        from datetime import datetime, timedelta

        future_date = (datetime.now() + timedelta(days=5)).strftime("%Y%m%d")
        risk_map = build_event_risk_map(
            unlocks=[{"code": "600001", "unlock_ratio": 12, "date": future_date}],
            pledges=[{"code": "600001", "pledge_ratio": 60}],
        )
        quotes = pd.DataFrame(
            [
                {"code": "600001", "name": "风险样本", "price": 10, "pct_chg": 2, "turnover": 200000000, "volume_ratio": 1.5},
                {"code": "600002", "name": "普通样本", "price": 10, "pct_chg": 2, "turnover": 200000000, "volume_ratio": 1.5},
            ]
        )
        candidates = attach_event_risk(prepare_candidates(quotes), {"status": "ok", "items": risk_map})
        rows, _ = score_tomorrow_candidates(candidates, top_n=2)
        risk_row = next(row for row in rows if row["code"] == "600001")
        normal_row = next(row for row in rows if row["code"] == "600002")

        self.assertGreater(risk_row["serenity_profile"]["risk_score"], normal_row["serenity_profile"]["risk_score"])
        self.assertTrue(any("事件风险" in reason for reason in risk_row["failure_reasons"]))

    def test_event_risk_ignores_stale_reduction_notice(self):
        from datetime import datetime, timedelta

        stale_date = (datetime.now() - timedelta(days=365)).strftime("%Y%m%d")
        recent_date = (datetime.now() - timedelta(days=5)).strftime("%Y%m%d")
        with patch.object(config, "EVENT_RISK_REDUCTION_LOOKBACK_DAYS", 120):
            risk_map = build_event_risk_map(
                reductions=[
                    {"code": "600001", "date": stale_date},
                    {"code": "600002", "date": recent_date},
                ]
            )

        self.assertNotIn("600001", risk_map)
        self.assertIn("600002", risk_map)

    def test_portfolio_gross_exposure_uses_regime_and_drawdown(self):
        rows = [
            {"rank": idx + 1, "code": "60000{}".format(idx), "name": "样本{}".format(idx), "theme": theme, "score": 80}
            for idx, theme in enumerate(["半导体", "算力", "军工", "医药"])
        ]
        perf = {"metrics": {"max_drawdown_pct": -10.0}}
        result = build_portfolio(
            rows,
            max_positions=4,
            single_cap=0.4,
            theme_cap=0.7,
            market_regime={"score": 35, "level": "risk_off"},
            performance=perf,
        )

        self.assertLess(result["summary"]["total_weight"], 50)
        self.assertGreater(result["summary"]["cash_pct"], 50)
        self.assertAlmostEqual(result["summary"]["regime_factor"], config.PORTFOLIO_GROSS_RISK_OFF, places=2)
        self.assertAlmostEqual(result["summary"]["drawdown_factor"], config.PORTFOLIO_DD_FACTOR_1, places=2)

    def test_fundamental_factors_and_factor_ic(self):
        df = pd.DataFrame(
            [
                {"code": "600001", "roe": 18, "gross_margin": 45, "debt_ratio": 25, "pe_dynamic": 12, "pb": 1.2, "earnings_surprise": 20},
                {"code": "600002", "roe": 5, "gross_margin": 18, "debt_ratio": 70, "pe_dynamic": 60, "pb": 8.0, "earnings_surprise": -10},
                {"code": "600003", "roe": 12, "gross_margin": 30, "debt_ratio": 45, "pe_dynamic": 25, "pb": 2.5, "earnings_surprise": 5},
            ]
        )
        with patch.object(config, "ENABLE_FUNDAMENTALS", True):
            enriched = attach_fundamental_factors(df)
        samples = [
            {"raw": {"fundamental_quality_score": row["fundamental_quality_score"]}, "primary_return_net": ret}
            for row, ret in zip(enriched.to_dict("records"), [3.0, -2.0, 1.0])
        ]
        ic = compute_factor_ic(samples, factor_keys=["fundamental_quality_score"])

        self.assertGreater(enriched.iloc[0]["fundamental_quality_score"], enriched.iloc[1]["fundamental_quality_score"])
        self.assertGreater(ic["ic"]["fundamental_quality_score"]["ic"], 0)

    def test_missing_fundamental_factors_are_neutral_not_top_ranked(self):
        df = pd.DataFrame(
            [
                {"code": "600001", "pe_dynamic": 0, "pb": 0},
                {"code": "600002", "pe_dynamic": 0, "pb": 0},
            ]
        )
        with patch.object(config, "ENABLE_FUNDAMENTALS", True):
            enriched = attach_fundamental_factors(df)

        self.assertTrue(enriched["fundamental_degraded"].all())
        self.assertEqual(enriched.iloc[0]["fundamental_quality_score"], 50.0)
        self.assertEqual(enriched.iloc[0]["earnings_surprise_score"], 50.0)

    def test_fundamental_loader_uses_daily_cache(self):
        import tempfile

        class FakeProvider:
            def __init__(self):
                self.calls = 0

            def get_fundamental_factors(self, codes=None):
                self.calls += 1
                return {"600001": {"roe": 18, "gross_margin": 40, "debt_ratio": 25, "pe_dynamic": 12, "pb": 1.5}}

        provider = FakeProvider()
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = "{}/fundamentals.json".format(tmpdir)
            with patch.object(config, "ENABLE_FUNDAMENTALS", True), patch.object(
                config, "FUNDAMENTAL_CACHE_PATH", cache_path
            ), patch.object(config, "FUNDAMENTAL_CACHE_HOURS", 24):
                first = load_fundamentals(provider, codes=["600001"])
                second = load_fundamentals(provider, codes=["600001"])

        self.assertEqual(first["status"], "ok")
        self.assertEqual(second["status"], "ok")
        self.assertEqual(provider.calls, 1)
        self.assertIn("600001", second["items"])

    def test_daily_job_factor_ic_writes_file(self):
        import io
        import json
        import sys
        import tempfile
        from contextlib import redirect_stdout
        from stock_analyzer import daily_job

        samples = [
            {"raw": {"fundamental_quality_score": 90}, "primary_return_net": 3.0},
            {"raw": {"fundamental_quality_score": 50}, "primary_return_net": 1.0},
            {"raw": {"fundamental_quality_score": 10}, "primary_return_net": -2.0},
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            factor_path = "{}/factor_ic.json".format(tmpdir)
            db_path = "{}/validation.sqlite3".format(tmpdir)
            argv = ["daily_job", "--factor-ic", "--strategy", "tomorrow_picks"]
            with patch.object(sys, "argv", argv), patch.object(config, "FACTOR_IC_PATH", factor_path), patch.object(
                config, "VALIDATION_DB_PATH", db_path
            ), patch(
                "stock_analyzer.daily_job.StrategyValidationStore.live_weight_samples",
                return_value=samples,
            ):
                with redirect_stdout(io.StringIO()):
                    result = daily_job.main()
            with open(factor_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)

        self.assertEqual(result, 0)
        self.assertGreater(payload["ic"]["fundamental_quality_score"]["ic"], 0)

    def test_factor_ic_weighting_can_adjust_combiner_when_enabled(self):
        import json
        import tempfile
        from stock_analyzer.scoring import _combine

        components = {
            "liquidity_score": 10,
            "momentum_score": 90,
            "trend_score": 50,
            "execution_score": 50,
            "risk_penalty": 0,
            "overheat_damp": 1,
        }
        baseline = _combine(components, "tomorrow_picks")
        payload = {
            "ic": {
                "momentum_score": {"ic": 1.0, "sample_count": 3, "status": "ok"},
                "liquidity_score": {"ic": -1.0, "sample_count": 3, "status": "ok"},
            }
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            path = "{}/factor_ic.json".format(tmpdir)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            with patch.object(config, "ENABLE_FACTOR_IC_WEIGHTING", True), patch.object(
                config, "FACTOR_IC_PATH", path
            ), patch.object(config, "FACTOR_IC_MIN_SAMPLES", 1):
                adjusted = _combine(components, "tomorrow_picks")

        self.assertGreater(adjusted, baseline)

    def test_portfolio_endpoint_uses_latest_saved_snapshot(self):
        import tempfile

        rows = [
            {
                "rank": idx + 1,
                "code": "60000{}".format(idx),
                "name": "样本{}".format(idx),
                "price": 10 + idx,
                "score": 90 - idx,
                "theme": ["半导体", "算力", "军工", "医药"][idx],
                "serenity_profile": {"confidence_score": 80 - idx, "risk_score": 40 + idx},
            }
            for idx in range(4)
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            validation_path = "{}/validation.sqlite3".format(tmpdir)
            StrategyValidationStore(validation_path).save_signals(
                "tomorrow_picks",
                "tomorrow_picks_v2",
                "2024-01-01T14:30:00",
                rows,
            )
            with patch.object(config, "VALIDATION_DB_PATH", validation_path), patch.object(
                config, "VALIDATION_AUTO_UPDATE_ENABLED", False
            ), patch.object(config, "PORTFOLIO_SINGLE_CAP", 0.4), patch.object(
                config, "PORTFOLIO_THEME_CAP", 0.7
            ):
                app = create_app()
                response = app.test_client().get("/api/portfolio?strategy=tomorrow_picks")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["summary"]["position_count"], 4)
        self.assertIn("suggested_weight", payload["data"][0])
        total_weight = sum(row["suggested_weight"] for row in payload["data"])
        self.assertAlmostEqual(total_weight, payload["summary"]["total_weight"], places=1)
        self.assertAlmostEqual(total_weight + payload["summary"]["cash_pct"], 100.0, places=1)
        self.assertIn("gross_exposure_pct", payload["summary"])

    def test_paper_trading_store_records_closed_trades_and_nav(self):
        import tempfile

        rows = [
            {
                "rank": 1,
                "code": "600001",
                "name": "盈利样本",
                "price": 10,
                "score": 90,
                "theme": "半导体",
                "turnover": 500000000,
                "serenity_profile": {"confidence_score": 80, "risk_score": 40},
            },
            {
                "rank": 2,
                "code": "600002",
                "name": "亏损样本",
                "price": 10,
                "score": 80,
                "theme": "算力",
                "turnover": 500000000,
                "serenity_profile": {"confidence_score": 70, "risk_score": 45},
            },
        ]
        histories = {
            "600001": pd.DataFrame(
                {
                    "trade_date": ["20240101", "20240102"],
                    "open": [10.0, 10.0],
                    "high": [10.1, 11.5],
                    "low": [9.9, 9.8],
                    "price": [10.0, 11.0],
                    "turnover": [500000000, 520000000],
                }
            ),
            "600002": pd.DataFrame(
                {
                    "trade_date": ["20240101", "20240102"],
                    "open": [10.0, 10.0],
                    "high": [10.1, 10.2],
                    "low": [9.9, 9.3],
                    "price": [10.0, 9.4],
                    "turnover": [500000000, 510000000],
                }
            ),
        }

        class FakeProvider:
            def get_history(self, code, days=220):
                return histories[code]

        with tempfile.TemporaryDirectory() as tmpdir:
            validation_path = "{}/validation.sqlite3".format(tmpdir)
            paper_path = "{}/paper.sqlite3".format(tmpdir)
            validation_store = StrategyValidationStore(validation_path)
            validation_store.save_signals("tomorrow_picks", "tomorrow_picks_v2", "2024-01-01T14:30:00", rows)
            paper_store = PaperTradingStore(paper_path)
            with patch.object(config, "PORTFOLIO_SINGLE_CAP", 0.6), patch.object(config, "PORTFOLIO_THEME_CAP", 0.8):
                result = paper_store.run_paper_trade(FakeProvider(), validation_store, "tomorrow_picks")
            performance = paper_store.performance("tomorrow_picks", days=20)
            trades = paper_store.trades("tomorrow_picks")

        self.assertTrue(result["ok"])
        self.assertEqual(result["saved"], 2)
        self.assertEqual({trade["status"] for trade in trades}, {"closed"})
        self.assertEqual(performance["metrics"]["closed_count"], 2)
        self.assertNotEqual(performance["metrics"]["total_return_pct"], 0.0)
        self.assertIn("max_drawdown_pct", performance["metrics"])

    def test_paper_trade_spreads_capital_by_holding_days(self):
        from stock_analyzer.paper_trading import _evaluate_trade

        history = pd.DataFrame(
            {
                "trade_date": ["20240101"] + ["202401{:02d}".format(day) for day in range(2, 13)],
                "open": [10.0] * 12,
                "high": [10.1] * 12,
                "low": [9.9] * 12,
                "price": [10.0] + [11.0] * 11,
                "turnover": [500000000] * 12,
            }
        )

        class FakeProvider:
            def get_history(self, code, days=220):
                return history

        with patch.object(config, "PAPER_TRADING_SPREAD_CAPITAL_BY_HOLDING_DAYS", True):
            trade = _evaluate_trade(
                FakeProvider(),
                "swing_picks",
                "2024-01-01",
                {"code": "600001", "name": "样本", "price": 10, "suggested_weight": 100, "turnover": 500000000},
            )

        self.assertEqual(trade["status"], "closed")
        self.assertLess(trade["weighted_return_pct"], trade["net_return_pct"])
        self.assertAlmostEqual(trade["weighted_return_pct"], trade["net_return_pct"] / 10, places=4)

    def test_portfolio_performance_endpoint_returns_paper_nav(self):
        import tempfile

        rows = [
            {
                "rank": 1,
                "code": "600001",
                "name": "盈利样本",
                "price": 10,
                "score": 90,
                "theme": "半导体",
                "turnover": 500000000,
                "serenity_profile": {"confidence_score": 80, "risk_score": 40},
            }
        ]
        history = pd.DataFrame(
            {
                "trade_date": ["20240101", "20240102"],
                "open": [10.0, 10.0],
                "high": [10.1, 11.5],
                "low": [9.9, 9.8],
                "price": [10.0, 11.0],
                "turnover": [500000000, 520000000],
            }
        )

        class FakeProvider:
            def get_history(self, code, days=220):
                return history

        with tempfile.TemporaryDirectory() as tmpdir:
            validation_path = "{}/validation.sqlite3".format(tmpdir)
            paper_path = "{}/paper.sqlite3".format(tmpdir)
            validation_store = StrategyValidationStore(validation_path)
            validation_store.save_signals("tomorrow_picks", "tomorrow_picks_v2", "2024-01-01T14:30:00", rows)
            with patch.object(config, "PORTFOLIO_SINGLE_CAP", 1.0), patch.object(config, "PORTFOLIO_THEME_CAP", 1.0):
                PaperTradingStore(paper_path).run_paper_trade(FakeProvider(), validation_store, "tomorrow_picks")
            with patch.object(config, "VALIDATION_DB_PATH", validation_path), patch.object(
                config, "PAPER_TRADING_DB_PATH", paper_path
            ), patch.object(config, "VALIDATION_AUTO_UPDATE_ENABLED", False):
                app = create_app()
                client = app.test_client()
                perf_response = client.get("/api/portfolio/performance?strategy=tomorrow_picks&days=20")
                trades_response = client.get("/api/paper-trades?strategy=tomorrow_picks")
                portfolio_response = client.get("/api/portfolio?strategy=tomorrow_picks")

        self.assertEqual(perf_response.status_code, 200)
        perf_payload = perf_response.get_json()
        self.assertTrue(perf_payload["ok"])
        self.assertEqual(perf_payload["performance"]["metrics"]["closed_count"], 1)
        self.assertEqual(trades_response.get_json()["data"][0]["status"], "closed")
        self.assertIn("performance", portfolio_response.get_json())

    def test_run_snapshot_saves_strategy_rows_without_web_route(self):
        import tempfile

        quotes = pd.DataFrame(
            [
                {
                    "code": "600001",
                    "name": "强势样本",
                    "price": 12,
                    "pct_chg": 4.2,
                    "speed": 1.2,
                    "volume_ratio": 1.8,
                    "turnover_rate": 4.5,
                    "turnover": 180000000,
                    "industry": "半导体",
                    "sixty_day_pct": 20,
                    "ytd_pct": 30,
                    "amplitude": 5,
                },
                {
                    "code": "600002",
                    "name": "稳健样本",
                    "price": 9,
                    "pct_chg": 2.0,
                    "speed": 0.4,
                    "volume_ratio": 1.2,
                    "turnover_rate": 2.5,
                    "turnover": 120000000,
                    "industry": "电力",
                    "sixty_day_pct": 8,
                    "ytd_pct": 12,
                    "amplitude": 3,
                },
            ]
        )

        class FakeProvider:
            def get_realtime_quotes(self):
                return quotes

        with tempfile.TemporaryDirectory() as tmpdir:
            store = StrategyValidationStore("{}/validation.sqlite3".format(tmpdir))
            with patch.object(config, "QUOTE_SNAPSHOT_MIN_ROWS", 1):
                result = run_snapshot(FakeProvider(), store, "tomorrow_picks", market="all")
            dates = store.list_signal_dates("tomorrow_picks")

        self.assertTrue(result["ok"])
        self.assertGreater(result["saved"]["saved"], 0)
        self.assertEqual(dates[0]["strategy_name"], "tomorrow_picks")

    def test_run_snapshot_rejects_local_quote_snapshot(self):
        import tempfile
        from datetime import datetime

        quotes = pd.DataFrame(
            [
                {
                    "code": "600{:03d}".format(index),
                    "name": "样本{}".format(index),
                    "price": 10,
                    "pct_chg": 2,
                    "speed": 0.4,
                    "volume_ratio": 1.5,
                    "turnover_rate": 4,
                    "turnover": 200000000,
                    "industry": "半导体",
                    "sixty_day_pct": 12,
                    "ytd_pct": 16,
                    "amplitude": 4,
                }
                for index in range(60)
            ]
        )

        class FakeProvider:
            def get_realtime_quotes(self):
                return quotes

            def health(self):
                return {
                    "quotes_source": "本地快照",
                    "last_quote_refresh": datetime.now().isoformat(timespec="seconds"),
                }

        with tempfile.TemporaryDirectory() as tmpdir:
            store = StrategyValidationStore("{}/validation.sqlite3".format(tmpdir))
            result = run_snapshot(FakeProvider(), store, "tomorrow_picks", market="all")
            dates = store.list_signal_dates("tomorrow_picks")

        self.assertFalse(result["ok"])
        self.assertEqual(result["saved"]["saved"], 0)
        self.assertEqual(dates, [])
        self.assertIn("本地快照", result["error"])

    def test_live_weight_calibration_keeps_weights_when_samples_insufficient(self):
        import tempfile
        from stock_analyzer.calibrate import calibrate_live_weights

        with tempfile.TemporaryDirectory() as tmpdir:
            weights_path = "{}/weights.json".format(tmpdir)
            db_path = "{}/validation.sqlite3".format(tmpdir)
            with patch.object(config, "WEIGHTS_OVERRIDE_PATH", weights_path), patch.object(
                config, "CALIBRATE_MIN_SAMPLES", 30
            ):
                result = calibrate_live_weights("tomorrow_picks", db_path=db_path, dry_run=False)

        self.assertEqual(result["status"], "insufficient_samples")
        self.assertFalse(os.path.exists(weights_path))

    def test_live_sample_evaluation_uses_alpha_not_absolute_beta(self):
        from stock_analyzer.calibrate import _evaluate_live_samples

        samples = []
        for idx, score in enumerate((90, 70, 50), start=1):
            samples.append(
                {
                    "signal_date": "2024-01-01",
                    "primary_return_net": 5.0,
                    "raw": {
                        "liquidity_score": score,
                        "momentum_score": score,
                        "trend_score": score,
                        "execution_score": score,
                        "risk_penalty": 0,
                    },
                }
            )

        metrics = _evaluate_live_samples(
            "tomorrow_picks",
            samples,
            {"liquidity": 0.25, "momentum": 0.25, "trend": 0.25, "execution": 0.25},
            top_k=1,
        )

        self.assertEqual(metrics["absolute_win_rate"], 100.0)
        self.assertEqual(metrics["absolute_avg_period_return"], 5.0)
        self.assertEqual(metrics["win_rate"], 0.0)
        self.assertEqual(metrics["avg_period_return"], 0.0)

    def test_tomorrow_objective_prefers_absolute_primary_metrics(self):
        from stock_analyzer.calibrate import _objective

        metrics = {
            "win_rate": 88.0,
            "avg_period_return": 4.0,
            "absolute_win_rate": 62.0,
            "absolute_avg_period_return": 1.5,
        }

        objective_tomorrow = _objective(metrics, "tomorrow_picks")
        objective_default = _objective(metrics)

        self.assertEqual(objective_tomorrow, 65.0)
        self.assertEqual(objective_default, 96.0)
        self.assertGreater(objective_default, objective_tomorrow)

    def test_tomorrow_objective_penalizes_downside_distribution(self):
        from stock_analyzer.calibrate import _objective

        stable = {
            "absolute_win_rate": 60.0,
            "absolute_avg_period_return": 1.0,
            "absolute_median_period_return": 0.8,
            "absolute_loss_quantile_return": 0.2,
            "absolute_avg_next_open_return": 0.1,
            "absolute_avg_max_drawdown": -1.0,
        }
        fragile = {
            **stable,
            "absolute_loss_quantile_return": -4.0,
            "absolute_avg_max_drawdown": -7.0,
        }

        self.assertGreater(_objective(stable, "tomorrow_picks"), _objective(fragile, "tomorrow_picks"))

    def test_compare_momentum_keeps_generic_objective(self):
        from stock_analyzer.calibrate import _objective

        metrics = {"win_rate": 88.0, "avg_period_return": 4.0}

        self.assertEqual(_objective(metrics), 96.0)

    def test_live_weight_calibration_requires_oos_improvement_to_write(self):
        import tempfile
        from stock_analyzer.calibrate import calibrate_live_weights

        samples = []
        for day in range(1, 5):
            for rank, score in enumerate((90, 60), start=1):
                samples.append(
                    {
                        "signal_date": "2024-01-{:02d}".format(day),
                        "primary_return_net": 1.0 if rank == 1 else 0.5,
                        "raw": {
                            "liquidity_score": score,
                            "momentum_score": score,
                            "trend_score": score,
                            "execution_score": score,
                            "risk_penalty": 0,
                            "serenity_profile": {"data_coverage": 1.0},
                        },
                    }
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            weights_path = "{}/weights.json".format(tmpdir)
            with patch.object(config, "WEIGHTS_OVERRIDE_PATH", weights_path), patch.object(
                config, "CALIBRATE_MIN_SAMPLES", 2
            ), patch(
                "stock_analyzer.calibrate.StrategyValidationStore.live_weight_samples",
                return_value=samples,
            ), patch(
                "stock_analyzer.calibrate._walk_forward_evaluate",
                return_value={
                    "ok": True,
                    "baseline_oos_objective": 10.0,
                    "best_oos_objective": 10.01,
                    "oos_improvement": 0.01,
                    "positive_folds": 1,
                    "fold_count": 4,
                    "folds": [],
                },
            ), patch("stock_analyzer.calibrate._write_weights_override") as writer:
                result = calibrate_live_weights("tomorrow_picks", db_path="{}/v.sqlite3".format(tmpdir), dry_run=False)

        self.assertEqual(result["status"], "no_oos_improvement")
        writer.assert_not_called()

    def test_live_weight_calibration_rejects_low_factor_coverage(self):
        import tempfile
        from stock_analyzer.calibrate import calibrate_live_weights

        samples = []
        for day in range(1, 5):
            for score in (90, 60):
                samples.append(
                    {
                        "signal_date": "2024-02-{:02d}".format(day),
                        "primary_return_net": 1.0,
                        "raw": {
                            "liquidity_score": score,
                            "momentum_score": score,
                            "trend_score": score,
                            "execution_score": score,
                            "risk_penalty": 0,
                        },
                    }
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(config, "CALIBRATE_MIN_SAMPLES", 2), patch.object(
                config, "CALIBRATE_MIN_COVERAGE", 0.5
            ), patch(
                "stock_analyzer.calibrate.StrategyValidationStore.live_weight_samples",
                return_value=samples,
            ):
                result = calibrate_live_weights("tomorrow_picks", db_path="{}/v.sqlite3".format(tmpdir), dry_run=False)

        self.assertEqual(result["status"], "insufficient_factor_coverage")
        self.assertEqual(result["avg_data_coverage"], 0.0)

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
        self.assertEqual(metrics["primary_horizon_label"], "次日开盘入场")
        self.assertEqual(metrics["avg_primary_return"], 4.1667)
        self.assertAlmostEqual(
            metrics["avg_primary_return_net"],
            4.1667 - metrics["avg_trade_cost_pct"],
        )

    def test_strategy_validation_stores_exit_rule_outcome(self):
        import tempfile

        class FakeProvider:
            def get_history(self, code, days=180):
                return pd.DataFrame(
                    {
                        "trade_date": ["20240101", "20240102", "20240103"],
                        "open": [10, 10.1, 10.4],
                        "high": [10.2, 10.3, 10.6],
                        "low": [9.8, 9.4, 10.1],
                        "price": [10, 10.2, 10.5],
                    }
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            store = StrategyValidationStore("{}/validation.sqlite3".format(tmpdir))
            store.save_signals(
                "tomorrow_picks",
                "tomorrow_picks_v2",
                "2024-01-01T14:30:00",
                [{"rank": 1, "code": "600001", "name": "止损样本", "price": 10, "score": 90}],
            )
            update = store.update_outcomes(FakeProvider(), signal_date="2024-01-01", strategy_name="tomorrow_picks")
            rows = store.signals_for_date("2024-01-01", "tomorrow_picks")
            metrics = store.metrics("tomorrow_picks", days=20)

        self.assertEqual(update["updated"], 1)
        self.assertEqual(rows[0]["exit_reason"], "stop_loss")
        self.assertAlmostEqual(rows[0]["signal_exit_return"], -5.0)
        self.assertAlmostEqual(metrics["avg_exit_return"], -5.0)
        self.assertAlmostEqual(metrics["avg_exit_return_net"], -5.0 - metrics["avg_trade_cost_pct"])

    def test_strategy_validation_skips_unbuyable_limit_up_sample(self):
        from stock_analyzer.strategy_validation import _compute_outcome

        class FakeProvider:
            def get_history(self, code, days=180):
                return pd.DataFrame(
                    {
                        "trade_date": ["20240101", "20240102", "20240103"],
                        "open": [10.0, 11.0, 11.2],
                        "high": [10.1, 11.0, 11.5],
                        "low": [9.9, 11.0, 11.1],
                        "price": [10.0, 11.0, 11.3],
                    }
                )

        signal = {
            "code": "600001",
            "signal_date": "2024-01-01",
            "price_at_signal": 10.0,
            "strategy_name": "tomorrow_picks",
            "market": "main",
        }

        outcome = _compute_outcome(FakeProvider(), signal)
        self.assertTrue(outcome["excluded"])
        self.assertEqual(outcome["skip_reason"], "unbuyable_limit_up")

    def test_validation_execution_cost_uses_liquidity_slippage(self):
        from stock_analyzer.strategy_validation import _execution_cost_pct

        liquid = _execution_cost_pct({"turnover": 1_500_000_000})
        illiquid = _execution_cost_pct({"turnover": 50_000_000})

        self.assertGreater(illiquid, liquid)
        self.assertGreater(illiquid, config.VALIDATION_TRADE_COST_PCT)

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
        self.assertEqual(metrics["primary_horizon_label"], "次日开盘入场")

    def test_tomorrow_validation_primary_metrics_ignore_backup_pool(self):
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
                "tomorrow_picks_v4",
                "2024-01-01T15:00:00",
                [
                    {
                        "rank": 1,
                        "code": "600001",
                        "name": "重点样本",
                        "price": 10,
                        "score": 90,
                        "tier": "primary_watch",
                    },
                    {
                        "rank": 11,
                        "code": "600002",
                        "name": "备选样本",
                        "price": 10,
                        "score": 70,
                        "tier": "backup_pool",
                    },
                ],
            )
            store.update_outcomes(FakeProvider(), strategy_name="tomorrow_picks")
            metrics = store.metrics("tomorrow_picks", days=20)
            samples = store.live_weight_samples("tomorrow_picks", days=20)

        self.assertEqual(metrics["sample_count"], 1)
        self.assertEqual(metrics["outcome_sample_count"], 1)
        self.assertEqual(metrics["total_sample_count"], 2)
        self.assertEqual(metrics["backup_sample_count"], 1)
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["code"], "600001")

    def test_tomorrow_primary_return_config_forces_open_entry(self):
        column, days, horizon = _primary_return_config("tomorrow_picks")
        self.assertEqual(column, "next_close_return")
        self.assertEqual(days, 1)
        self.assertEqual(horizon, "次日开盘入场")
        with patch.object(config, "VALIDATION_PRIMARY_ENTRY_MODE", "signal"):
            column_signal, days_signal, horizon_signal = _primary_return_config("tomorrow_picks")
            self.assertEqual(column_signal, "next_close_return")
            self.assertEqual(days_signal, 1)
            self.assertEqual(horizon_signal, "次日开盘入场")

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
        self.assertEqual(metrics["primary_horizon_label"], "20日开盘入场")
        self.assertEqual(metrics["outcome_sample_count"], 2)
        self.assertEqual(metrics["sample_count"], 1)
        self.assertEqual(metrics["real_sample_count"], 1)
        self.assertAlmostEqual(metrics["avg_primary_return"], 18.8119)
        self.assertAlmostEqual(
            metrics["avg_primary_return_net"],
            18.8119 - metrics["avg_trade_cost_pct"],
        )
        self.assertEqual(metrics["daily"][0]["sample_count"], 1)
        self.assertAlmostEqual(
            metrics["daily"][0]["avg_primary_return_net"],
            18.8119 - metrics["avg_trade_cost_pct"],
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

    def test_stock_prediction_endpoint_uses_history_when_realtime_quote_missing(self):
        import tempfile

        quotes = pd.DataFrame(
            [
                {
                    "code": "600001",
                    "name": "其他股票",
                    "price": 10,
                    "pct_chg": 1.0,
                    "volume_ratio": 1.0,
                    "turnover": 500000000,
                    "sixty_day_pct": 5,
                    "ytd_pct": 8,
                    "industry": "银行",
                }
            ]
        )
        history = pd.DataFrame(
            [
                {"trade_date": "2026-01-02", "price": 10.0, "volume": 1000000, "turnover": 80000000},
                {"trade_date": "2026-01-03", "price": 10.5, "volume": 1200000, "turnover": 90000000},
                {"trade_date": "2026-01-04", "price": 10.2, "volume": 1100000, "turnover": 85000000},
            ]
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(config, "STATE_PATH", "{}/state.json".format(tmpdir)), patch.object(
                config, "VALIDATION_DB_PATH", "{}/validation.sqlite3".format(tmpdir)
            ), patch.object(config, "ENABLE_HISTORY_FACTORS", False), patch(
                "stock_analyzer.app.MarketDataProvider.get_realtime_quotes",
                return_value=quotes,
            ), patch(
                "stock_analyzer.app.MarketDataProvider.get_history",
                return_value=history,
            ):
                app = create_app()
                client = app.test_client()
                response = client.get("/api/stock-prediction/600999")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["filtered"])
        self.assertEqual(payload["data_source"], "历史行情兜底")
        self.assertEqual(payload["prediction"]["direction"], "down")
        self.assertIn("实时行情源未返回", "；".join(payload["risk_flags"]))
        self.assertGreater(payload["price"], 0)

    def test_stock_prediction_endpoint_returns_diagnosis_when_all_quotes_missing(self):
        import tempfile

        def fail_quotes(self):
            raise RuntimeError("实时源不可用")

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(config, "STATE_PATH", "{}/state.json".format(tmpdir)), patch.object(
                config, "VALIDATION_DB_PATH", "{}/validation.sqlite3".format(tmpdir)
            ), patch.object(config, "ENABLE_HISTORY_FACTORS", False), patch(
                "stock_analyzer.app.MarketDataProvider.get_realtime_quotes",
                fail_quotes,
            ), patch(
                "stock_analyzer.app.MarketDataProvider.get_history",
                return_value=pd.DataFrame(),
            ):
                app = create_app()
                client = app.test_client()
                response = client.get("/api/stock-prediction/600999")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["filtered"])
        self.assertEqual(payload["data_source"], "无可用行情")
        self.assertEqual(payload["prediction"]["direction"], "down")
        self.assertIn("历史行情也不可用", "；".join(payload["risk_flags"]))
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

    def test_chokepoint_endpoint_returns_industry_map_when_no_matches(self):
        import tempfile

        quotes = pd.DataFrame(
            [
                {
                    "code": "600000",
                    "name": "普通银行",
                    "price": 10,
                    "pct_chg": 0.5,
                    "turnover": 500000000,
                    "turnover_rate": 2,
                    "volume_ratio": 1.0,
                    "sixty_day_pct": 3,
                    "ytd_pct": 5,
                    "amplitude": 2,
                    "industry": "银行",
                },
                {
                    "code": "300308",
                    "name": "中际旭创",
                    "price": 100,
                    "pct_chg": 2.1,
                    "turnover": 900000000,
                    "turnover_rate": 6,
                    "volume_ratio": 2.0,
                    "sixty_day_pct": 15,
                    "ytd_pct": 30,
                    "amplitude": 5,
                    "industry": "通信服务",
                },
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
                response = client.get("/api/chokepoint-picks?top_n=10")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"], [])
        industry_map = payload["meta"]["industry_map"]
        self.assertTrue(industry_map)
        totals = industry_map[0]["totals"]
        self.assertGreater(totals["industry_count"], 0)
        self.assertGreater(totals["leader_count"], 0)
        self.assertGreater(totals["unique_leader_count"], 0)
        self.assertLessEqual(totals["unique_leader_count"], totals["leader_count"])
        self.assertEqual(totals["quote_available_count"], 1)
        optical = next(node for node in industry_map if node["segment"] == "光器件")
        leader = next(item for item in optical["leaders"] if item["code"] == "300308")
        self.assertTrue(leader["quote_available"])
        self.assertFalse(leader["matched"])
        self.assertIn(leader["recommendation"]["level"], {"observe", "avoid"})
        self.assertIn("empty_reason", payload["meta"])

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

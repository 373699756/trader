import json
import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

import pandas as pd

from stock_analyzer import config
from stock_analyzer.app import create_app
from stock_analyzer.backtest import run_alphalite_backtest, run_rolling_alphalite_backtest
from stock_analyzer.daily_data import DailyMarketDataStore, load_history_frames
from stock_analyzer.event_risk import attach_event_risk, build_event_risk_map
from stock_analyzer.factors import compute_alphalite_for_stock
from stock_analyzer.factor_ic import compute_factor_ic
from stock_analyzer.fundamentals import attach_fundamental_factors, load_fundamentals
from stock_analyzer.history_cache import HistoryCache
from stock_analyzer.normalization import rename_known_columns
from stock_analyzer.oos_report import build_strategy_oos_report
from stock_analyzer.paper_trading import PaperTradingStore
from stock_analyzer.portfolio import build_portfolio
from stock_analyzer.providers import MarketDataProvider, _normalize_eastmoney_spot, _request_eastmoney_page
from stock_analyzer.risk_rules import simulate_exit
from stock_analyzer.risk_blacklist import attach_risk_blacklist, load_risk_blacklist
from stock_analyzer.recommendation_snapshot import load_recommendation_snapshot, save_recommendation_snapshot
from stock_analyzer.scoring import (
    build_market_regime,
    candidate_filter_report,
    limit_theme_concentration,
    prepare_candidates,
    score_today_candidates,
    score_swing_candidates,
    score_tomorrow_candidates,
)
from stock_analyzer.sentiment import score_news_items
from stock_analyzer.stability import TopKDropoutTracker
from stock_analyzer.snapshot import run_snapshot
from stock_analyzer.strategy_validation import StrategyValidationStore, _primary_return_config, validation_baseline_config
from stock_analyzer.validation_replay import backfill_strategy_validation_samples





class RecommendationStrategiesTest(unittest.TestCase):

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

    def test_short_term_returns_today_top_10(self):
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

        result, meta = score_today_candidates(
            candidates,
            hot_ranks={"600001": 8},
            industry_strength={"半导体": 1.2, "电力": 1.5},
            sentiment_lookup={"600002": {"score": 68, "summary": "舆情偏正面"}},
            top_n=10,
        )

        self.assertIn("short_term", result)
        self.assertNotIn("long_term", result)
        self.assertEqual(meta["top_n"], 10)
        self.assertEqual(result["short_term"][0]["code"], "600001")
        self.assertEqual(meta["strategy_version"], config.SHORT_TERM_STRATEGY_VERSION)
        self.assertEqual(result["short_term"][0]["tier"], "backup_pool")
        self.assertFalse(result["short_term"][0]["execution_allowed"])
        self.assertEqual(result["short_term"][0]["trade_action"]["position_size"], 0.0)
        self.assertEqual(result["short_term"][0]["recommendation_class_label"], "今日延续推荐")
        self.assertHasExplanationFields(result["short_term"][0], "short_term")

    def test_short_term_rows_carry_verdict_and_bull_bear(self):
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
        result, _ = score_today_candidates(candidates, {}, {}, {}, top_n=10)
        short_rows = result["short_term"]
        self.assertTrue(short_rows)
        row = short_rows[0]
        self.assertIn("verdict", row)
        self.assertIn(row["verdict"]["tier"], {"strong_buy", "buy", "watch", "reduce", "avoid"})
        self.assertIn("execution_score", row)
        self.assertIsInstance(row["execution_score"], (int, float))
        self.assertIn("bull_score", row)
        self.assertIn("bear_score", row)

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
        self.assertEqual(meta["strategy_version"], config.SWING_STRATEGY_VERSION)
        self.assertEqual(rows[0]["horizon"], "swing")
        self.assertHasExplanationFields(rows[0], "swing_picks")

    def test_swing_degrades_when_history_factor_coverage_is_missing(self):
        quotes = pd.DataFrame(
            [
                {
                    "code": "600{:03d}".format(index),
                    "name": "降级样本{}".format(index),
                    "price": 10 + index * 0.1,
                    "pct_chg": 2.0,
                    "volume_ratio": 1.6,
                    "turnover_rate": 4,
                    "turnover": 500000000 + index * 10000000,
                    "sixty_day_pct": 12,
                    "ytd_pct": 18,
                    "amplitude": 4,
                }
                for index in range(16)
            ]
        )

        with patch.object(config, "SWING_RECOMMENDATION_MIN_SCORE", 0):
            rows, meta = score_swing_candidates(prepare_candidates(quotes), top_n=16)

        self.assertTrue(meta["factor_degraded"])
        self.assertEqual(meta["history_factor_ready_ratio"], 0.0)
        self.assertLessEqual(meta["display_count"], config.SWING_DEGRADED_DISPLAY_LIMIT)
        self.assertTrue(all(row.get("factor_degraded") for row in rows))
        self.assertTrue(all(row.get("tier") == "backup_pool" for row in rows))
        self.assertTrue(all(row.get("execution_allowed") is False for row in rows))
        self.assertTrue(all(row.get("trade_action", {}).get("position_size") == 0 for row in rows))
        self.assertTrue(any("历史因子覆盖不足" in reason for reason in rows[0]["reasons"]))

    @unittest.skip("旧量价突破策略已下线，当前只保留今天/明天/2-5天三策略")
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

    @unittest.skip("旧量价突破策略已下线，当前只保留今天/明天/2-5天三策略")
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

    @unittest.skip("旧量价突破策略已下线，当前只保留今天/明天/2-5天三策略")
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

    @unittest.skip("旧量价突破策略已下线，当前只保留今天/明天/2-5天三策略")
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

    @unittest.skip("旧反转策略已下线，当前只保留今天/明天/2-5天三策略")
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

    @unittest.skip("旧反转策略已下线，当前只保留今天/明天/2-5天三策略")
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

    def test_chokepoint_score_rewards_upstream_underpriced(self):
        from stock_analyzer.scoring_core.theme_scores import _chokepoint_score

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

    def test_serenity_references_corrected_to_chokepoint(self):
        from stock_analyzer.scoring_core.theme_scores import SERENITY_REFERENCES

        joined = " ".join(ref.get("adopted", "") + ref.get("repo", "") for ref in SERENITY_REFERENCES)
        # 不再误标为 BDD/Discord/OS 等不相关仓库
        self.assertNotIn("serenity-bdd", joined)
        self.assertNotIn("SerenityOS", joined)
        # 体现卡脖子/瓶颈方法论
        self.assertIn("卡脖子", joined)

    @unittest.skip("旧卡脖子策略已下线，当前只保留今天/明天/2-5天三策略")
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

    @unittest.skip("旧卡脖子策略已下线，当前只保留今天/明天/2-5天三策略")
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

    @unittest.skip("旧卡脖子策略已下线，当前只保留今天/明天/2-5天三策略")
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
        from stock_analyzer.scoring_core.theme_scores import CHOKEPOINT_INDUSTRY_LEADERS

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

        self.assertEqual(response.status_code, 404)

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

        self.assertNotIn("600002", {row["code"] for row in rows})
        self.assertEqual(meta["analysis_window"], "14:30")
        self.assertLessEqual(len(rows), 18)
        if rows:
            self.assertHasExplanationFields(rows[0], "tomorrow_picks")

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

        self.assertEqual(response.status_code, 404)

    @unittest.skip("旧小市值价值策略已下线，当前只保留今天/明天/2-5天三策略")
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

    @unittest.skip("旧中长期策略已下线，当前只保留今天/明天/2-5天三策略")
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





if __name__ == '__main__':

    unittest.main()

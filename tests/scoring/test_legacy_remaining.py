import json
import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

import pandas as pd

from helpers import score_tech_potential_candidates, validation_history as _validation_history
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




class LegacyRemainingScoringTest(unittest.TestCase):
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

    def test_verdict_tier_bands_and_coverage_gate(self):
        from stock_analyzer.scoring_core.explanations import _verdict_tier

        # 高分低风险 + 覆盖充足 → strong_buy
        self.assertEqual(_verdict_tier(85, 30, 0.9)["tier"], "strong_buy")
        # 低分 → avoid
        self.assertEqual(_verdict_tier(20, 40, 0.9)["tier"], "avoid")
        # A4：高分但历史因子覆盖不足 → 强制降级到 watch，且带 note
        gated = _verdict_tier(85, 30, 0.2)
        self.assertEqual(gated["tier"], "watch")
        self.assertEqual(gated["note"], "历史因子覆盖不足，评级降级")

    def test_data_coverage_uses_factor_metadata_not_nonzero_values(self):
        from stock_analyzer.scoring_core.explanations import _data_coverage

        row = pd.Series({"alphalite_coverage": 0.67, "ret_20d": 0.0, "breakout_20d": 0.0})
        self.assertAlmostEqual(_data_coverage(row), 0.67)

    def test_factor_coverage_alerts_when_alphalite_is_silent(self):
        from stock_analyzer.selfcheck import factor_coverage

        candidates = pd.DataFrame(
            [
                {"code": "600001", "alphalite_factor_ready": 0, "alphalite_coverage": 0.0},
                {"code": "600002", "alphalite_factor_ready": 0, "alphalite_coverage": 0.0},
                {"code": "600003", "alphalite_factor_ready": 1, "alphalite_coverage": 1.0, "ret_20d": 2.0},
            ]
        )

        with patch.object(config, "FACTOR_COVERAGE_ALERT_ZERO_RATIO", 0.30):
            coverage = factor_coverage(candidates)

        alert_codes = {alert["code"] for alert in coverage["alerts"]}
        self.assertTrue(coverage["degraded"])
        self.assertEqual(coverage["alphalite_ready_ratio"], 0.3333)
        self.assertIn("alphalite_coverage_zero", alert_codes)
        self.assertIn("alphalite_factor_not_ready", alert_codes)

    def test_overheat_damp_suppresses_extended_names(self):
        from stock_analyzer.scoring_core.scoring_math import _apply_overheat_damp, _overheat_damp_multiplier

        calm = pd.Series({"sixty_day_pct": 5, "ytd_pct": 10, "amplitude": 4})
        extended = pd.Series({"sixty_day_pct": 130, "ytd_pct": 160, "amplitude": 15})
        # 过热票的 final 被乘法压低，明显低于温和票。
        self.assertLess(_apply_overheat_damp(80, extended), _apply_overheat_damp(80, calm))
        self.assertLessEqual(_apply_overheat_damp(80, extended), 80)
        self.assertLess(_overheat_damp_multiplier(extended), _overheat_damp_multiplier(calm))

    def test_overheat_is_not_repeated_in_tomorrow_risk_penalty(self):
        from stock_analyzer.scoring_core.risk import _tomorrow_risk_penalty_parts
        from stock_analyzer.scoring_core.scoring_math import _overheat_damp_multiplier

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

    def test_validation_baseline_config_exposes_cost_and_survivorship_switches(self):
        with patch.object(config, "ENABLE_TAIL_AUCTION_SLIPPAGE", True), patch.object(
            config, "ENABLE_MARKET_IMPACT", True
        ), patch.object(config, "ENABLE_SURVIVORSHIP_CORRECTION", True):
            baseline = validation_baseline_config("tomorrow_picks")

        self.assertEqual(baseline["primary_return_field"], "signal_exit_return")
        self.assertEqual(baseline["net_return_formula"], "signal_exit_return - trade_cost_pct")
        self.assertFalse(baseline["cost_model"]["tail_auction_slippage_enabled"])
        self.assertTrue(baseline["cost_model"]["market_impact_enabled"])
        self.assertTrue(baseline["survivorship"]["enabled"])
        self.assertTrue(baseline["separate_legacy_baseline_required"])
        self.assertIn("survivorship", baseline["baseline_id"])

    def test_attach_validation_summary_exposes_validation_baseline(self):
        from stock_analyzer.app_support import attach_validation_summary

        baseline = validation_baseline_config("tomorrow_picks")
        rows = [{"code": "600001", "score": 80}]
        attach_validation_summary(
            rows,
            validation_store=object(),
            strategy_name="tomorrow_picks",
            metrics_fn=lambda strategy, days: {
                "sample_count": 1,
                "validation_baseline": baseline,
                "validation_baseline_id": baseline["baseline_id"],
                "current_baseline_outcome_count": 4,
                "raw_outcome_sample_count": 5,
                "legacy_baseline_outcome_count": 3,
                "excluded_baseline_mismatch_count": 1,
                "avg_trade_cost_pct": 0.37,
                "survivorship_corrected_count": 2,
            },
        )

        stats = rows[0]["similar_signal_stats"]
        self.assertEqual(stats["validation_baseline_id"], baseline["baseline_id"])
        self.assertEqual(stats["validation_baseline"]["net_return_formula"], "signal_exit_return - trade_cost_pct")
        self.assertEqual(stats["current_baseline_outcome_count"], 4)
        self.assertEqual(stats["raw_outcome_sample_count"], 5)
        self.assertEqual(stats["legacy_baseline_outcome_count"], 3)
        self.assertEqual(stats["excluded_baseline_mismatch_count"], 1)
        self.assertEqual(stats["avg_trade_cost_pct"], 0.37)
        self.assertEqual(stats["survivorship_corrected_count"], 2)

    def test_smooth_penalty_reduces_tomorrow_threshold_jump(self):
        from stock_analyzer.scoring_core.risk import _tomorrow_risk_penalty_parts

        base = pd.Series(
            {
                "pct_chg": 2.0,
                "market": "main",
                "turnover_rate": 5.0,
                "volume_ratio": 1.5,
                "speed": 0.0,
            }
        )
        below = pd.Series({**base.to_dict(), "amplitude": 10.9})
        above = pd.Series({**base.to_dict(), "amplitude": 11.1})

        with patch.object(config, "USE_SMOOTH_PENALTY", True):
            below_parts = _tomorrow_risk_penalty_parts(below)
            above_parts = _tomorrow_risk_penalty_parts(above)

        self.assertIn("amplitude", below_parts)
        self.assertIn("amplitude", above_parts)
        self.assertLess(above_parts["amplitude"] - below_parts["amplitude"], 1.0)

    def test_smooth_penalty_can_restore_legacy_thresholds(self):
        from stock_analyzer.scoring_core.risk import _tomorrow_risk_penalty_parts

        row = pd.Series(
            {
                "pct_chg": 2.0,
                "market": "main",
                "amplitude": 11.1,
                "turnover_rate": 5.0,
                "volume_ratio": 1.5,
                "speed": 0.0,
            }
        )

        with patch.object(config, "USE_SMOOTH_PENALTY", False):
            parts = _tomorrow_risk_penalty_parts(row)

        self.assertEqual(parts["amplitude"], 10)

    def test_recommendation_display_caps_inferred_theme_when_industry_missing(self):
        rows = [
            {"code": "688{:03d}".format(index), "name": "芯片推荐{}".format(index), "score": 90 - index, "industry": ""}
            for index in range(6)
        ]

        selected, limited = limit_theme_concentration(rows, limit=6, cap=3)

        self.assertEqual(len(selected), 3)
        self.assertEqual(limited, 3)

    def test_recommendation_display_rotates_themes_before_second_pick(self):
        rows = [
            {"code": "600001", "name": "半导体一", "theme": "半导体", "score": 99},
            {"code": "600002", "name": "半导体二", "theme": "半导体", "score": 98},
            {"code": "600003", "name": "算力一", "theme": "AI/算力", "score": 90},
            {"code": "600004", "name": "新能源一", "theme": "新能源", "score": 80},
        ]

        selected, limited = limit_theme_concentration(rows, limit=4, cap=3)

        self.assertEqual([row["code"] for row in selected], ["600001", "600003", "600004", "600002"])
        self.assertEqual(limited, 0)

    def test_strategy_status_uses_trading_days_for_every_strategy(self):
        from stock_analyzer.strategy_health import strategy_status

        status = strategy_status(
            {
                "strategy_name": "short_term",
                "sample_count": 25,
                "day_count": 2,
                "real_sample_count": 25,
                "real_day_count": 2,
                "real_win_rate_primary_net": 55.0,
                "real_avg_primary_return_net": 0.6,
                "avg_max_drawdown_3d": -1.0,
            }
        )

        self.assertEqual(status["state"], "pending")

    def test_weights_override_loads_from_json(self):
        import json
        import tempfile
        import importlib

        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/weights.json"
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"thresholds": {"min_data_coverage": 0.99}}, handle)
            with patch.object(config, "WEIGHTS_OVERRIDE_PATH", path), patch.object(
                config, "PRODUCTION_FREEZE_ENABLED", False
            ):
                from stock_analyzer.scoring_core.weights import _load_weight_overrides

                weights, thresholds = _load_weight_overrides()
                self.assertEqual(thresholds["min_data_coverage"], 0.99)
            # 不存在文件时回退默认
            with patch.object(config, "WEIGHTS_OVERRIDE_PATH", f"{tmp}/missing.json"), patch.object(
                config, "PRODUCTION_FREEZE_ENABLED", False
            ):
                weights, thresholds = _load_weight_overrides()
                self.assertEqual(thresholds["min_data_coverage"], 0.5)

    def test_bear_score_defaults_neutral_when_committee_missing(self):
        from stock_analyzer.scoring_core.explanations import _attach_signal_explanation

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
            {"thresholds": {"verdict": 80}},
            {"thresholds": {"min_data_coverage": -1}},
            {"thresholds": {"overheat_damp_floor": 2}},
        ]
        for bad in cases:
            with tempfile.TemporaryDirectory() as tmp:
                path = f"{tmp}/weights.json"
                with open(path, "w", encoding="utf-8") as handle:
                    json.dump(bad, handle)
                with patch.object(config, "WEIGHTS_OVERRIDE_PATH", path):
                    from stock_analyzer.scoring_core.weights import _load_weight_overrides

                    _, thresholds = _load_weight_overrides()
                    self.assertIsInstance(thresholds["verdict"], dict)
                    self.assertTrue(0.0 <= thresholds["min_data_coverage"] <= 1.0)
                    self.assertTrue(0.0 <= thresholds["overheat_damp_floor"] <= 1.0)

    def test_alphalite_attach_uses_cache_only_by_default_to_avoid_request_blocking(self):
        from stock_analyzer.app_support import attach_alphalite_factors as _attach_alphalite_factors
        from stock_analyzer.providers import TimedCache

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

    def test_alphalite_attach_schedules_missing_history_without_sync_fetch(self):
        import threading

        from stock_analyzer.app_support import attach_alphalite_factors as _attach_alphalite_factors
        from stock_analyzer.providers import TimedCache

        quotes = pd.DataFrame(
            [
                {
                    "code": "600001",
                    "name": "样本",
                    "price": 20,
                    "pct_chg": 3.0,
                    "turnover": 9e8,
                    "turnover_rate": 7,
                    "volume_ratio": 2.0,
                    "sixty_day_pct": 18,
                    "amplitude": 5,
                }
            ]
        )
        refreshed = threading.Event()

        class BackgroundProvider:
            def get_cached_history(self, code, days=90):
                return pd.DataFrame()

            def get_history(self, code, days=90):
                raise AssertionError("request path must not synchronously fetch history")

            def prefetch_history(self, codes, days=90):
                refreshed.set()
                return {"downloaded": len(codes)}

        with patch.object(config, "ENABLE_HISTORY_FACTORS", True), patch.object(
            config, "HISTORY_FACTORS_FETCH_ON_REQUEST", True
        ):
            enriched = _attach_alphalite_factors(
                BackgroundProvider(),
                TimedCache(1),
                prepare_candidates(quotes),
            )

        self.assertEqual(enriched.iloc[0]["alphalite_factor_ready"], 0.0)
        self.assertTrue(refreshed.wait(timeout=1.0))

    def test_alphalite_attach_reuses_factor_cache_for_same_history(self):
        from stock_analyzer.app_support import attach_alphalite_factors as _attach_alphalite_factors
        from stock_analyzer.providers import TimedCache

        quotes = pd.DataFrame(
            [
                {"code": "600001", "name": "样本", "price": 20, "pct_chg": 3.0, "turnover": 9e8,
                 "turnover_rate": 7, "volume_ratio": 2.0, "sixty_day_pct": 18, "amplitude": 5},
            ]
        )
        history = pd.DataFrame(
            [
                {"trade_date": "20260701", "code": "600001", "price": 18.0, "high": 18.2, "turnover": 8e8, "volume": 1000},
                {"trade_date": "20260702", "code": "600001", "price": 19.0, "high": 19.2, "turnover": 8.5e8, "volume": 1100},
                {"trade_date": "20260703", "code": "600001", "price": 20.0, "high": 20.2, "turnover": 9e8, "volume": 1200},
                {"trade_date": "20260704", "code": "600001", "price": 20.5, "high": 20.6, "turnover": 9.2e8, "volume": 1300},
                {"trade_date": "20260705", "code": "600001", "price": 20.8, "high": 21.0, "turnover": 9.4e8, "volume": 1400},
                {"trade_date": "20260706", "code": "600001", "price": 21.0, "high": 21.1, "turnover": 9.6e8, "volume": 1500},
            ]
        )
        cache = TimedCache(60)

        class LocalProvider:
            def get_cached_history(self, code, days=90):
                return pd.DataFrame()

        with patch.object(config, "ENABLE_HISTORY_FACTORS", True), patch.object(
            config, "HISTORY_FACTORS_FETCH_ON_REQUEST", False
        ), patch(
            "stock_analyzer.app_support.load_local_history_frames",
            return_value={"600001": history},
        ), patch(
            "stock_analyzer.app_support.build_alphalite_factors",
            wraps=__import__("stock_analyzer.app_support", fromlist=["build_alphalite_factors"]).build_alphalite_factors,
        ) as build_mock:
            first = _attach_alphalite_factors(LocalProvider(), cache, prepare_candidates(quotes))
            second = _attach_alphalite_factors(LocalProvider(), cache, prepare_candidates(quotes))

        self.assertEqual(build_mock.call_count, 1)
        self.assertEqual(first.iloc[0]["alphalite_factor_ready"], second.iloc[0]["alphalite_factor_ready"])

    def test_sentiment_for_candidates_returns_stale_cache_without_blocking(self):
        from stock_analyzer.app_support import sentiment_for_candidates
        from stock_analyzer.providers import TimedCache

        cache = TimedCache(60)
        cache.set(
            {
                "entries": {
                    "600001": {
                        "value": {"score": 71.0, "summary": "旧缓存", "risk_words": []},
                        "expires_at": 1.0,
                    }
                },
                "refreshing": set(),
            }
        )

        with patch.object(config, "ENABLE_INLINE_SENTIMENT", True), patch(
            "stock_analyzer.app_support.threading.Thread.start",
            return_value=None,
        ) as start_mock, patch(
            "stock_analyzer.app_support.score_stock_sentiment",
            side_effect=AssertionError("stale cache should be returned immediately"),
        ):
            lookup = sentiment_for_candidates(
                object(),
                cache,
                [{"code": "600001", "name": "样本"}],
            )

        self.assertEqual(lookup["600001"]["score"], 71.0)
        self.assertEqual(lookup["600001"]["summary"], "旧缓存")
        self.assertEqual(start_mock.call_count, 1)

    def test_sentiment_for_candidates_returns_placeholder_for_missing_cache(self):
        from stock_analyzer.app_support import sentiment_for_candidates
        from stock_analyzer.providers import TimedCache

        cache = TimedCache(60)

        with patch.object(config, "ENABLE_INLINE_SENTIMENT", True), patch(
            "stock_analyzer.app_support.threading.Thread.start",
            return_value=None,
        ) as start_mock, patch(
            "stock_analyzer.app_support.score_stock_sentiment",
            side_effect=AssertionError("missing cache should not block on sync sentiment fetch"),
        ):
            lookup = sentiment_for_candidates(
                object(),
                cache,
                [{"code": "600001", "name": "样本"}],
            )

        self.assertEqual(lookup["600001"]["score"], 50.0)
        self.assertEqual(lookup["600001"]["summary"], "舆情刷新中")
        self.assertEqual(start_mock.call_count, 1)

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

    def test_enhanced_alphalite_factors_are_feature_gated(self):
        history = pd.DataFrame(
            {
                "trade_date": [f"d{i}" for i in range(25)],
                "code": "600001",
                "open": [10 + i * 0.2 for i in range(25)],
                "high": [10.4 + i * 0.2 for i in range(25)],
                "low": [9.8 + i * 0.2 for i in range(25)],
                "price": [10.2 + i * 0.2 for i in range(25)],
                "turnover": [1e8] * 25,
                "volume": [1e6] * 25,
            }
        )

        with patch.object(config, "ENABLE_ENHANCED_FACTORS", False):
            disabled = compute_alphalite_for_stock("600001", history)
        with patch.object(config, "ENABLE_ENHANCED_FACTORS", True):
            enabled = compute_alphalite_for_stock("600001", history)

        self.assertEqual(disabled["close_vs_vwap"], 0.0)
        self.assertGreater(enabled["price_position_20d"], 50.0)
        self.assertGreater(enabled["consecutive_up_days"], 0)
        self.assertGreater(enabled["amplitude_5d_mean"], 0.0)
        self.assertIn("upper_wick_ratio", enabled)
        self.assertIn("lower_wick_ratio", enabled)

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

    def test_load_history_frames_falls_back_to_sibling_sharded_store(self):
        import tempfile

        raw = pd.DataFrame(
            {
                "trade_date": ["20240101", "20240102"],
                "open": [10, 11],
                "high": [10.5, 11.5],
                "low": [9.8, 10.8],
                "close": [10.2, 11.2],
                "volume": [1000, 1200],
                "turnover": [10000000, 12000000],
                "pct_chg": [0.0, 9.8],
            }
        )
        qfq = raw.copy()
        qfq["open"] = [9, 10]
        qfq["high"] = [9.5, 10.5]
        qfq["low"] = [8.8, 9.8]
        qfq["close"] = [9.2, 10.2]
        with tempfile.TemporaryDirectory() as tmpdir:
            empty_sqlite = "{}/market_data.sqlite3".format(tmpdir)
            sharded_dir = "{}/market_data".format(tmpdir)
            DailyMarketDataStore(empty_sqlite)
            store = DailyMarketDataStore(sharded_dir)
            store.upsert_bars("600001", raw, qfq)

            history = load_history_frames(empty_sqlite, ["600001"], days=90)

        self.assertIn("600001", history)
        self.assertEqual(len(history["600001"]), 2)
        self.assertEqual(history["600001"].iloc[-1]["open"], 10)
        self.assertEqual(history["600001"].iloc[-1]["price"], 10.2)

    def test_market_data_fetch_history_falls_back_to_sina(self):
        from stock_analyzer import market_data

        class EmptyAk:
            def stock_zh_a_hist(self, **kwargs):
                raise RuntimeError("akshare failed")

        sina_history = pd.DataFrame(
            [
                {
                    "trade_date": "20240102",
                    "open": 10,
                    "close": 10.2,
                    "high": 10.5,
                    "low": 9.8,
                    "volume": 1000,
                    "turnover": 0,
                    "pct_chg": 0,
                }
            ]
        )
        with patch("stock_analyzer.market_data._fetch_eastmoney_history", side_effect=RuntimeError("eastmoney failed")):
            with patch("stock_analyzer.market_data._fetch_sina_history", return_value=sina_history) as sina:
                history = market_data._fetch_history(EmptyAk(), "600001", "20240101", "20240131", "qfq")

        self.assertEqual(history.iloc[0]["trade_date"], "20240102")
        sina.assert_called_once_with("600001", "20240101", "20240131")

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

    def test_strategy_validation_records_empty_saved_batch(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            store = StrategyValidationStore("{}/validation.sqlite3".format(tmpdir))
            result = store.save_signals(
                "tomorrow_picks",
                "tomorrow_picks_v5",
                "2026-07-08T15:00:00",
                [],
            )
            dates = store.list_signal_dates("tomorrow_picks")
            rows = store.signals_for_date("2026-07-08", "tomorrow_picks")
            latest_rows = store.latest_signal_rows("tomorrow_picks")

        self.assertEqual(result["saved"], 0)
        self.assertEqual(dates[0]["signal_date"], "2026-07-08")
        self.assertEqual(dates[0]["count"], 0)
        self.assertEqual(dates[0]["sample_type"], "empty")
        self.assertEqual(rows, [])
        self.assertEqual(latest_rows, [])

    def test_latest_signal_rows_ignores_newer_replay_batch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StrategyValidationStore("{}/validation.sqlite3".format(tmpdir))
            store.save_signals(
                "tomorrow_picks",
                config.TOMORROW_STRATEGY_VERSION,
                "2026-07-08T15:00:00",
                [{"rank": 1, "code": "600001", "name": "正式样本", "price": 10, "score": 80}],
            )
            store.save_signals(
                "tomorrow_picks",
                "tomorrow_picks_{}".format(config.VALIDATION_REPLAY_VERSION_SUFFIX),
                "2026-07-09T15:00:00",
                [{"rank": 1, "code": "600002", "name": "回放样本", "price": 10, "score": 80}],
            )

            latest_rows = store.latest_signal_rows("tomorrow_picks")

        self.assertEqual([row["code"] for row in latest_rows], ["600001"])
        self.assertEqual(latest_rows[0]["strategy_version"], config.TOMORROW_STRATEGY_VERSION)

    def test_strategy_validation_prunes_inactive_strategies(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            store = StrategyValidationStore("{}/validation.sqlite3".format(tmpdir))
            store.save_signals(
                "tomorrow_picks",
                "tomorrow_picks_v5",
                "2026-07-08T15:00:00",
                [{"rank": 1, "code": "600001", "name": "保留", "price": 10, "score": 80}],
            )
            store.save_signals(
                "position_picks",
                "position_1_3m_v1",
                "2026-07-08T15:00:00",
                [{"rank": 1, "code": "600002", "name": "删除", "price": 11, "score": 70}],
            )
            result = store.prune_strategies(("short_term", "tomorrow_picks", "swing_picks"))
            active_dates = store.list_signal_dates("tomorrow_picks")
            inactive_dates = store.list_signal_dates("position_picks")

        self.assertEqual(result["deleted_signals"], 1)
        self.assertEqual(active_dates[0]["count"], 1)
        self.assertEqual(inactive_dates, [])

    def test_strategy_validation_saves_tuning_run(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            store = StrategyValidationStore("{}/validation.sqlite3".format(tmpdir))
            plan = {
                "status": "blocked",
                "can_apply": False,
                "shadow_mode": True,
                "suggestions": [{"parameter": "min_score", "value": "+2"}],
            }
            saved = store.save_tuning_run("tomorrow_picks", 20, plan, {"sample_count": 0})
            latest = store.latest_tuning_run("tomorrow_picks")

        self.assertGreater(saved["id"], 0)
        self.assertEqual(latest["strategy_name"], "tomorrow_picks")
        self.assertEqual(latest["plan"]["status"], "blocked")
        self.assertFalse(latest["can_apply"])
        self.assertTrue(latest["shadow_mode"])

    def test_strategy_validation_tuning_endpoint_creates_shadow_plan(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = "{}/validation.sqlite3".format(tmpdir)
            StrategyValidationStore(db_path).save_signals(
                "tomorrow_picks",
                "tomorrow_picks_v5",
                "2026-07-08T15:00:00",
                [],
            )
            with patch.object(config, "VALIDATION_DB_PATH", db_path), patch.object(
                config, "VALIDATION_AUTO_UPDATE_ENABLED", False
            ), patch.object(config, "VALIDATION_AUTO_SNAPSHOT_ENABLED", False), patch.object(
                config, "ENABLE_DEEPSEEK_RUNTIME", False
            ):
                app = create_app()
                response = app.test_client().post("/api/strategy-validation/tuning?strategy=tomorrow_picks&days=20")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["plan"]["shadow_mode"])
        self.assertFalse(payload["plan"]["can_apply"])
        self.assertIn(payload["plan"]["status"], {"blocked", "shadow_only"})

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

    def test_factor_ic_weighting_can_adjust_combiner_when_enabled(self):
        import json
        import tempfile
        from stock_analyzer.scoring_core.scoring_math import _combine

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

    def test_time_decay_objective_rewards_recent_win_rate(self):
        from stock_analyzer.calibrate import _time_decay_multiplier

        improving = {
            "return_series_with_dates": [
                ("2024-01-01", -1.0),
                ("2024-01-02", -1.0),
                ("2024-04-01", 1.0),
                ("2024-04-02", 1.0),
            ]
        }
        deteriorating = {
            "return_series_with_dates": [
                ("2024-01-01", 1.0),
                ("2024-01-02", 1.0),
                ("2024-04-01", -1.0),
                ("2024-04-02", -1.0),
            ]
        }

        with patch.object(config, "CALIBRATE_USE_TIME_DECAY", True), patch.object(
            config, "CALIBRATE_TIME_DECAY_HALF_LIFE", 30
        ):
            self.assertGreater(_time_decay_multiplier(improving), _time_decay_multiplier(deteriorating))

    def test_compare_momentum_keeps_generic_objective(self):
        from stock_analyzer.calibrate import _objective

        metrics = {"win_rate": 88.0, "avg_period_return": 4.0}

        self.assertEqual(_objective(metrics), 96.0)

    def test_interaction_ranker_reports_oos_shadow_improvement(self):
        from stock_analyzer.calibrate import evaluate_interaction_ranker

        samples = []
        for day in range(1, 11):
            date = "2024-04-{:02d}".format(day)
            samples.append(
                {
                    "signal_date": date,
                    "stored_score": 65,
                    "primary_return_net": 2.0,
                    "next_open_return": 0.6,
                    "max_drawdown": -0.8,
                    "raw": {
                        "score": 65,
                        "liquidity_score": 95,
                        "momentum_score": 95,
                        "historical_edge_score": 35,
                        "execution_score": 35,
                        "tail_setup_score": 35,
                        "risk_penalty": 0,
                        "overheat_damp": 1,
                    },
                }
            )
            samples.append(
                {
                    "signal_date": date,
                    "stored_score": 78,
                    "primary_return_net": -1.5,
                    "next_open_return": -0.4,
                    "max_drawdown": -4.0,
                    "raw": {
                        "score": 78,
                        "liquidity_score": 35,
                        "momentum_score": 100,
                        "historical_edge_score": 95,
                        "execution_score": 95,
                        "tail_setup_score": 95,
                        "risk_penalty": 0,
                        "overheat_damp": 1,
                    },
                }
            )

        with patch.object(config, "CALIBRATE_WALK_FORWARD_FOLDS", 3), patch.object(
            config, "INTERACTION_MIN_TRAIN_SAMPLES", 4
        ), patch.object(config, "INTERACTION_MIN_ABS_CORR", 0.0), patch.object(
            config, "ENABLE_CALIBRATE_FDR", False
        ), patch.object(config, "CALIBRATE_IMPROVE_MARGIN", 0.01):
            result = evaluate_interaction_ranker("tomorrow_picks", samples, top_k=1, max_pairs=10)

        self.assertTrue(result["ok"])
        self.assertEqual(result["fold_count"], 3)
        self.assertEqual(result["status"], "oos_passed")
        self.assertGreater(result["interaction_oos_objective"], result["baseline_oos_objective"])
        self.assertGreater(result["positive_folds"], result["fold_count"] // 2)
        pairs = {item["pair"] for item in result["selected_interactions"]}
        self.assertIn("momentum_score*liquidity_score", pairs)
        self.assertFalse(result["fdr"]["enabled"])

    def test_interaction_ranker_requires_oos_folds(self):
        from stock_analyzer.calibrate import evaluate_interaction_ranker

        samples = [
            {
                "signal_date": "2024-04-01",
                "stored_score": 70,
                "primary_return_net": 1.0,
                "raw": {
                    "score": 70,
                    "liquidity_score": 70,
                    "momentum_score": 70,
                    "historical_edge_score": 70,
                    "execution_score": 70,
                    "tail_setup_score": 70,
                    "risk_penalty": 0,
                },
            },
            {
                "signal_date": "2024-04-02",
                "stored_score": 68,
                "primary_return_net": -0.5,
                "raw": {
                    "score": 68,
                    "liquidity_score": 68,
                    "momentum_score": 68,
                    "historical_edge_score": 68,
                    "execution_score": 68,
                    "tail_setup_score": 68,
                    "risk_penalty": 0,
                },
            },
        ]

        result = evaluate_interaction_ranker("tomorrow_picks", samples, top_k=1)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "insufficient_oos_folds")

    def test_regime_specific_weights_report_oos_shadow_improvement(self):
        from stock_analyzer.calibrate import evaluate_regime_specific_weights

        samples = []
        for day in range(1, 13):
            regime = "risk_on" if day % 2 else "risk_off"
            date = "2024-05-{:02d}".format(day)
            good_raw = {
                "regime_level": regime,
                "score": 75,
                "historical_edge_score": 50,
                "risk_penalty": 0,
                "overheat_damp": 1,
            }
            bad_raw = dict(good_raw)
            if regime == "risk_on":
                good_raw.update(
                    {
                        "liquidity_score": 50,
                        "momentum_score": 95,
                        "execution_score": 50,
                        "tail_setup_score": 50,
                    }
                )
                bad_raw.update(
                    {
                        "liquidity_score": 95,
                        "momentum_score": 35,
                        "execution_score": 50,
                        "tail_setup_score": 50,
                    }
                )
            else:
                good_raw.update(
                    {
                        "liquidity_score": 95,
                        "momentum_score": 50,
                        "execution_score": 50,
                        "tail_setup_score": 50,
                    }
                )
                bad_raw.update(
                    {
                        "liquidity_score": 50,
                        "momentum_score": 95,
                        "execution_score": 50,
                        "tail_setup_score": 95,
                    }
                )
            samples.append(
                {
                    "signal_date": date,
                    "stored_score": 75,
                    "primary_return_net": 2.0,
                    "max_drawdown": -1.0,
                    "raw": good_raw,
                }
            )
            samples.append(
                {
                    "signal_date": date,
                    "stored_score": 75,
                    "primary_return_net": -1.5,
                    "max_drawdown": -4.0,
                    "raw": bad_raw,
                }
            )

        with patch.object(config, "CALIBRATE_WALK_FORWARD_FOLDS", 3), patch.object(
            config, "REGIME_SPECIFIC_MIN_TRAIN_SAMPLES", 2
        ), patch.object(config, "CALIBRATE_IMPROVE_MARGIN", 0.01), patch.object(
            config, "ENABLE_CALIBRATE_FDR", False
        ):
            result = evaluate_regime_specific_weights("tomorrow_picks", samples, top_k=1, steps=3)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "oos_passed")
        self.assertGreater(result["regime_oos_objective"], result["baseline_oos_objective"])
        self.assertIn("risk_on", result["weights_by_regime"])
        self.assertIn("risk_off", result["weights_by_regime"])
        self.assertGreater(result["weights_by_regime"]["risk_on"]["momentum"], 0.20)
        self.assertGreater(result["weights_by_regime"]["risk_off"]["liquidity"], result["weights_by_regime"]["risk_off"]["momentum"])

    def test_regime_specific_weights_fallback_when_state_samples_insufficient(self):
        from stock_analyzer.calibrate import evaluate_regime_specific_weights

        samples = []
        for day in range(1, 7):
            samples.append(
                {
                    "signal_date": "2024-05-{:02d}".format(day),
                    "stored_score": 70,
                    "primary_return_net": 1.0,
                    "raw": {
                        "regime_level": "risk_on",
                        "score": 70,
                        "liquidity_score": 70,
                        "momentum_score": 70,
                        "historical_edge_score": 70,
                        "execution_score": 70,
                        "tail_setup_score": 70,
                        "risk_penalty": 0,
                    },
                }
            )

        with patch.object(config, "CALIBRATE_WALK_FORWARD_FOLDS", 3), patch.object(
            config, "REGIME_SPECIFIC_MIN_TRAIN_SAMPLES", 20
        ):
            result = evaluate_regime_specific_weights("tomorrow_picks", samples, top_k=1, steps=1)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "shadow_only")
        self.assertEqual(result["weights_by_regime"], {})
        self.assertIn("risk_on", result["fallback_regimes"])

    def test_benjamini_hochberg_fdr_selects_significant_configs(self):
        from stock_analyzer.calibrate import benjamini_hochberg_fdr, calibrate_with_fdr_guard

        fdr = benjamini_hochberg_fdr([0.001, 0.02, 0.2, 0.8], q=0.1)

        self.assertEqual(fdr["rejected"], [0, 1])
        prefix_fdr = benjamini_hochberg_fdr([0.03, 0.049], q=0.05)
        self.assertEqual(prefix_fdr["rejected"], [0, 1])
        result = calibrate_with_fdr_guard(
            [
                {"name": "weak"},
                {"name": "best"},
                {"name": "noise"},
            ],
            lambda cfg: {
                "p_value": {"weak": 0.02, "best": 0.01, "noise": 0.7}[cfg["name"]],
                "objective": {"weak": 1.0, "best": 2.0, "noise": 10.0}[cfg["name"]],
            },
            q=0.1,
        )

        self.assertEqual(result["status"], "selected")
        self.assertEqual(result["selected"]["name"], "best")

    def test_strategy_validation_metrics_use_stored_trade_cost_label(self):
        import tempfile

        class FakeProvider:
            def get_history(self, code, days=180):
                return _validation_history("2024-01-01", future_days=3, final_price=10.6)

        with tempfile.TemporaryDirectory() as tmpdir:
            store = StrategyValidationStore("{}/validation.sqlite3".format(tmpdir))
            store.save_signals(
                "tomorrow_picks",
                config.TOMORROW_STRATEGY_VERSION,
                "2024-01-01T14:30:00",
                [
                    {
                        "rank": 1,
                        "code": "600001",
                        "name": "成本样本",
                        "price": 10,
                        "score": 90,
                        "turnover": 50_000_000,
                    }
                ],
            )
            with patch.object(config, "ENABLE_TAIL_AUCTION_SLIPPAGE", True), patch.object(
                config, "ENABLE_MARKET_IMPACT", False
            ), patch.object(config, "TAIL_AUCTION_MAX_EXTRA_SLIPPAGE_PCT", 0.8), patch.object(
                config, "VALIDATION_PORTFOLIO_CAPITAL", 1_000_000
            ):
                store.update_outcomes(FakeProvider(), signal_date="2024-01-01", strategy_name="tomorrow_picks")
                rows = store.signals_for_date("2024-01-01", "tomorrow_picks")
                stored_cost = rows[0]["trade_cost_pct"]
                metrics = store.metrics("tomorrow_picks", days=20)
                samples = store.live_weight_samples("tomorrow_picks", days=20)
            with patch.object(config, "ENABLE_TAIL_AUCTION_SLIPPAGE", True), patch.object(
                config, "ENABLE_MARKET_IMPACT", False
            ), patch.object(config, "TAIL_AUCTION_MAX_EXTRA_SLIPPAGE_PCT", 0.0), patch.object(
                config, "VALIDATION_PORTFOLIO_CAPITAL", 1_000
            ):
                changed_policy_metrics = store.metrics("tomorrow_picks", days=20)
                changed_policy_samples = store.live_weight_samples("tomorrow_picks", days=20)

        self.assertGreater(stored_cost, config.VALIDATION_TRADE_COST_PCT)
        self.assertAlmostEqual(metrics["avg_trade_cost_pct"], stored_cost)
        self.assertAlmostEqual(samples[0]["trade_cost_pct"], stored_cost)
        self.assertAlmostEqual(
            metrics["avg_primary_return_net"],
            metrics["avg_primary_return"] - stored_cost,
        )
        self.assertEqual(changed_policy_metrics["sample_count"], 0)
        self.assertEqual(changed_policy_metrics["excluded_baseline_mismatch_count"], 1)
        self.assertEqual(changed_policy_samples, [])

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
                config.TOMORROW_STRATEGY_VERSION,
                "2024-01-01T14:30:00",
                [{"rank": 1, "code": "600001", "name": "止损样本", "price": 10, "score": 90}],
            )
            update = store.update_outcomes(FakeProvider(), signal_date="2024-01-01", strategy_name="tomorrow_picks")
            rows = store.signals_for_date("2024-01-01", "tomorrow_picks")
            metrics = store.metrics("tomorrow_picks", days=20)

        self.assertEqual(update["updated"], 1)
        self.assertEqual(rows[0]["exit_reason"], "stop_loss")
        self.assertAlmostEqual(rows[0]["overnight_return"], 1.0)
        self.assertAlmostEqual(rows[0]["signal_exit_return"], -5.0)
        self.assertAlmostEqual(metrics["avg_exit_return"], -5.0)
        self.assertAlmostEqual(metrics["avg_exit_return_net"], -5.0 - metrics["avg_trade_cost_pct"])

    def test_strategy_validation_skips_unbuyable_limit_up_at_next_open(self):
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
        self.assertEqual(outcome["label_status"], "settled")
        self.assertAlmostEqual(outcome["overnight_return"], 10.0)

    def test_strategy_validation_skips_high_open_chase(self):
        from stock_analyzer.strategy_validation import _compute_outcome

        class FakeProvider:
            def get_history(self, code, days=180):
                return pd.DataFrame(
                    {
                        "trade_date": ["20240101", "20240102", "20240103"],
                        "open": [10.0, 10.31, 10.2],
                        "high": [10.1, 10.5, 10.4],
                        "low": [9.9, 10.1, 10.0],
                        "price": [10.0, 10.2, 10.3],
                    }
                )

        signal = {
            "code": "600001",
            "signal_date": "2024-01-01",
            "price_at_signal": 10.0,
            "strategy_name": "tomorrow_picks",
            "market": "main",
        }

        with patch.object(config, "TOMORROW_HIGH_OPEN_SKIP_PCT", 3.0):
            outcome = _compute_outcome(FakeProvider(), signal)

        self.assertEqual(outcome["label_status"], "settled")
        self.assertGreater(outcome["next_open_return"], 3.0)

    def test_strategy_validation_stale_no_future_without_evidence_is_unknown(self):
        import tempfile

        class FakeProvider:
            def get_history(self, code, days=180):
                return pd.DataFrame(
                    {
                        "trade_date": ["20240101"],
                        "open": [10.0],
                        "high": [10.2],
                        "low": [9.8],
                        "price": [10.0],
                    }
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            store = StrategyValidationStore("{}/validation.sqlite3".format(tmpdir))
            store.save_signals(
                "tomorrow_picks",
                config.TOMORROW_STRATEGY_VERSION,
                "2024-01-01T15:00:00",
                [{"rank": 1, "code": "600001", "name": "待回填样本", "price": 10, "score": 90}],
            )
            with patch.object(config, "ENABLE_SURVIVORSHIP_CORRECTION", False):
                update = store.update_outcomes(FakeProvider(), signal_date="2024-01-01", strategy_name="tomorrow_picks")
                rows = store.signals_for_date("2024-01-01", "tomorrow_picks")
                metrics = store.metrics("tomorrow_picks", days=20)

        self.assertEqual(update["updated"], 0)
        self.assertEqual(update["skipped"], 1)
        self.assertEqual(update["pending"], 0)
        self.assertEqual(update["unknown"], 1)
        self.assertEqual(update["skipped_reasons"]["post_1430_entry_waiting_future_trade"], 1)
        self.assertEqual(rows[0]["label_status"], "unknown")
        self.assertIsNone(rows[0]["outcome_updated_at"])
        self.assertEqual(metrics["sample_count"], 0)

    def test_strategy_validation_survivorship_correction_preserves_observed_future_days(self):
        import tempfile

        class FakeProvider:
            def get_security_status(self, code):
                return {"status": "delisted"}

            def get_history(self, code, days=180):
                return pd.DataFrame(
                    {
                        "trade_date": ["20240101", "20240102", "20240103"],
                        "open": [10.0, 10.0, 10.1],
                        "high": [10.2, 10.2, 10.3],
                        "low": [9.8, 9.9, 10.0],
                        "price": [10.0, 10.1, 10.2],
                    }
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            store = StrategyValidationStore("{}/validation.sqlite3".format(tmpdir))
            store.save_signals(
                "swing_picks",
                config.SWING_STRATEGY_VERSION,
                "2024-01-01T15:00:00",
                [{"rank": 1, "code": "600001", "name": "停牌样本", "price": 10, "score": 90}],
            )
            with patch.object(config, "ENABLE_SURVIVORSHIP_CORRECTION", True), patch.object(
                config, "SURVIVORSHIP_CORRECTION_STALE_DAYS", 0
            ):
                update = store.update_outcomes(FakeProvider(), signal_date="2024-01-01", strategy_name="swing_picks")
                rows = store.signals_for_date("2024-01-01", "swing_picks")
                metrics = store.metrics("swing_picks", days=20)

        self.assertEqual(update["updated"], 1)
        self.assertEqual(update["unknown"], 0)
        self.assertEqual(update["pending"], 0)
        self.assertEqual(rows[0]["future_days"], 2)
        self.assertEqual(rows[0]["survivorship_corrected"], 1)
        self.assertEqual(rows[0]["correction_reason"], "delisted_last_tradable_liquidation")
        self.assertEqual(metrics["sample_count"], 1)
        self.assertEqual(metrics["survivorship_corrected_count"], 1)
        self.assertEqual(metrics["survivor_sample_count"], 0)

    def test_validation_execution_cost_uses_liquidity_slippage(self):
        from stock_analyzer.strategy_validation import _execution_cost_pct

        liquid = _execution_cost_pct({"turnover": 1_500_000_000})
        illiquid = _execution_cost_pct({"turnover": 50_000_000})

        self.assertEqual(illiquid, liquid)

    def test_validation_execution_cost_adds_tail_auction_slippage_when_enabled(self):
        from stock_analyzer.strategy_validation import _execution_cost_pct, tail_auction_slippage_pct

        row = {"turnover": 50_000_000, "suggested_weight": 10}
        with patch.object(config, "ENABLE_TAIL_AUCTION_SLIPPAGE", False), patch.object(
            config, "ENABLE_MARKET_IMPACT", False
        ):
            baseline = _execution_cost_pct(row)
        with patch.object(config, "ENABLE_TAIL_AUCTION_SLIPPAGE", True), patch.object(
            config, "TAIL_AUCTION_LIQUIDITY_RATIO", 0.05
        ), patch.object(config, "TAIL_AUCTION_MAX_EXTRA_SLIPPAGE_PCT", 0.8), patch.object(
            config, "VALIDATION_PORTFOLIO_CAPITAL", 1_000_000
        ), patch.object(
            config, "ENABLE_MARKET_IMPACT", False
        ):
            adjusted = _execution_cost_pct(row)
            tail_with_base = tail_auction_slippage_pct(row, base_slippage=0.2)

        self.assertEqual(adjusted, baseline)
        self.assertEqual(tail_with_base, 0.0)

    def test_validation_execution_cost_adds_market_impact_when_enabled(self):
        from stock_analyzer.strategy_validation import _execution_cost_pct, market_impact_cost_pct

        row = {"turnover": 100_000_000, "adv_20d": 100_000_000, "suggested_weight": 10}
        with patch.object(config, "ENABLE_MARKET_IMPACT", False), patch.object(
            config, "ENABLE_TAIL_AUCTION_SLIPPAGE", False
        ):
            baseline = _execution_cost_pct(row)
        with patch.object(config, "ENABLE_MARKET_IMPACT", True), patch.object(
            config, "VALIDATION_PORTFOLIO_CAPITAL", 10_000_000
        ), patch.object(config, "MARKET_IMPACT_COEFFICIENT", 0.1), patch.object(
            config, "MARKET_IMPACT_MAX_COST_PCT", 5.0
        ), patch.object(config, "ENABLE_TAIL_AUCTION_SLIPPAGE", False):
            impact = market_impact_cost_pct(row)
            adjusted = _execution_cost_pct(row)

        self.assertEqual(impact, 0)
        self.assertAlmostEqual(adjusted, baseline + impact)

    def test_backtest_trade_cost_reuses_validation_cost_model(self):
        from stock_analyzer.backtest import _backtest_trade_cost_pct
        from stock_analyzer.strategy_validation import _execution_cost_pct

        with patch.object(config, "ENABLE_TAIL_AUCTION_SLIPPAGE", False), patch.object(
            config, "ENABLE_MARKET_IMPACT", False
        ):
            self.assertEqual(_backtest_trade_cost_pct(100_000_000), _execution_cost_pct({"turnover": 100_000_000}))

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
                config.TOMORROW_STRATEGY_VERSION,
                "2024-01-01T14:30:00",
                [{"rank": 1, "code": "600001", "name": "真实样本", "price": 10, "score": 90}],
            )
            store.save_signals(
                "tomorrow_picks",
                "tomorrow_picks_{}".format(config.VALIDATION_REPLAY_VERSION_SUFFIX),
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
        self.assertEqual(metrics["primary_horizon_label"], "T日14:30后参考入场至T+1规则退出")

    def test_swing_pending_counts_only_use_current_formal_version(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StrategyValidationStore("{}/validation.sqlite3".format(tmpdir))
            store.save_signals(
                "swing_picks",
                "swing_2_5d_v1",
                "2024-01-01T15:00:00",
                [{"rank": 1, "code": "600001", "name": "旧版本", "price": 10, "score": 90}],
            )
            store.save_signals(
                "swing_picks",
                config.SWING_STRATEGY_VERSION,
                "2024-01-02T15:00:00",
                [{"rank": 1, "code": "600002", "name": "当前版本", "price": 10, "score": 90}],
            )

            metrics = store.metrics("swing_picks", days=20)

        self.assertEqual(metrics["strategy_version"], config.SWING_STRATEGY_VERSION)
        self.assertEqual(metrics["signal_sample_count"], 1)
        self.assertEqual(metrics["pending_outcome_count"], 1)

    def test_only_incomplete_outcome_update_skips_mature_rows(self):
        import tempfile

        histories = {
            "600001": _validation_history("2024-01-01", future_days=6, final_price=10.8),
            "600002": _validation_history("2024-02-01", future_days=6, final_price=10.6),
        }

        class FakeProvider:
            def __init__(self):
                self.calls = []

            def get_history(self, code, days=180):
                self.calls.append(code)
                return histories[code]

        provider = FakeProvider()
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StrategyValidationStore("{}/validation.sqlite3".format(tmpdir))
            store.save_signals(
                "tomorrow_picks",
                config.TOMORROW_STRATEGY_VERSION,
                "2024-01-01T15:00:00",
                [{"rank": 1, "code": "600001", "name": "成熟", "price": 10, "score": 90}],
            )
            store.update_outcomes(provider, strategy_name="tomorrow_picks")
            provider.calls.clear()
            mature_update = store.update_outcomes(
                provider,
                strategy_name="tomorrow_picks",
                only_incomplete=True,
            )
            store.save_signals(
                "tomorrow_picks",
                config.TOMORROW_STRATEGY_VERSION,
                "2024-02-01T15:00:00",
                [{"rank": 1, "code": "600002", "name": "待回填", "price": 10, "score": 90}],
            )
            pending_update = store.update_outcomes(
                provider,
                strategy_name="tomorrow_picks",
                only_incomplete=True,
            )

        self.assertEqual(mature_update["updated"], 0)
        self.assertEqual(pending_update["updated"], 1)
        self.assertEqual(provider.calls, ["600002"])

    def test_swing_validation_requires_mature_future_days(self):
        import tempfile

        histories = {
            "600001": _validation_history("2024-01-01", future_days=4, final_price=10.4),
            "600002": _validation_history("2024-01-01", future_days=5, final_price=11.0),
        }

        class FakeProvider:
            def get_history(self, code, days=180):
                return histories[code]

        with tempfile.TemporaryDirectory() as tmpdir, patch.object(
            config,
            "ENABLE_SURVIVORSHIP_CORRECTION",
            False,
        ), patch("stock_analyzer.strategy_validation._survivorship_correction_enabled", return_value=False):
            store = StrategyValidationStore("{}/validation.sqlite3".format(tmpdir))
            store.save_signals(
                "swing_picks",
                config.SWING_STRATEGY_VERSION,
                "2024-01-01T14:30:00",
                [
                    {"rank": 1, "code": "600001", "name": "未成熟样本", "price": 10, "score": 90},
                    {"rank": 2, "code": "600002", "name": "成熟样本", "price": 10, "score": 80},
                ],
            )
            update = store.update_outcomes(FakeProvider(), strategy_name="swing_picks")
            rows = store.signals_for_date("2024-01-01", "swing_picks")
            metrics = store.metrics("swing_picks", days=20)

        self.assertEqual(update["updated"], 1)
        self.assertEqual(update["unknown"], 0)
        self.assertEqual(update["pending"], 1)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["label_status"], "pending")
        self.assertEqual(rows[1]["label_status"], "settled")
        self.assertFalse(any(row.get("survivorship_corrected") for row in rows))
        self.assertEqual(metrics["primary_holding_days"], 5)
        self.assertEqual(metrics["primary_horizon_label"], "T日14:30后参考入场至T+2-T+5规则退出")
        self.assertEqual(metrics["outcome_sample_count"], 1)
        self.assertEqual(metrics["sample_count"], 1)
        self.assertEqual(metrics["real_sample_count"], 1)
        self.assertAlmostEqual(metrics["avg_primary_return"], 8.0)
        self.assertAlmostEqual(
            metrics["avg_primary_return_net"],
            8.0 - metrics["avg_trade_cost_pct"],
        )
        self.assertEqual(metrics["daily"][0]["sample_count"], 1)
        self.assertAlmostEqual(
            metrics["daily"][0]["avg_primary_return_net"],
            8.0 - metrics["avg_trade_cost_pct"],
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

    def test_backfill_samples_endpoint_grows_validation_sample_count(self):
        import tempfile

        dates = pd.date_range("2024-01-01", periods=75, freq="D").strftime("%Y%m%d").tolist()
        prices = [10 * (1.008 ** i) for i in range(75)]
        history = pd.DataFrame(
            {
                "trade_date": dates,
                "open": [value * 0.994 for value in prices],
                "high": [value * 1.004 for value in prices],
                "low": [value * 0.992 for value in prices],
                "price": prices,
                "turnover": [150000000 + i * 900000 for i in range(75)],
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
        self.assertEqual(
            payload["replay"]["version"],
            "tomorrow_picks_{}".format(config.VALIDATION_REPLAY_VERSION_SUFFIX),
        )

    def test_strategy_validation_runtime_config_reports_baseline_status(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            validation_path = "{}/validation.sqlite3".format(tmpdir)
            store = StrategyValidationStore(validation_path)
            store.save_signals(
                "tomorrow_picks",
                config.TOMORROW_STRATEGY_VERSION,
                "2024-01-01T14:30:00",
                [{"rank": 1, "code": "600001", "name": "待回填", "price": 10, "score": 90}],
            )
            with patch.object(config, "VALIDATION_DB_PATH", validation_path), patch.object(
                config, "STATE_PATH", "{}/state.json".format(tmpdir)
            ):
                app = create_app()
                response = app.test_client().get(
                    "/api/strategy-validation/runtime-config?strategy=tomorrow_picks&days=20"
                )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["validation_baseline_id"], payload["validation_baseline"]["baseline_id"])
        self.assertEqual(payload["baseline_status"]["status"], "needs_backfill")
        self.assertEqual(payload["baseline_status"]["signal_count"], 1)
        self.assertEqual(payload["baseline_status"]["pending_current_baseline_count"], 1)

    def test_strategy_validation_daily_summary_ignores_pending_outcomes(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = "{}/validation.sqlite3".format(tmpdir)
            store = StrategyValidationStore(db_path)
            store.save_signals(
                "tomorrow_picks",
                "tomorrow_picks_v5",
                "2026-07-08T15:00:00",
                [
                    {"rank": 1, "code": "600001", "name": "未回填A", "price": 10, "score": 80},
                    {"rank": 2, "code": "600002", "name": "未回填B", "price": 11, "score": 79},
                ],
            )
            with patch.object(config, "VALIDATION_DB_PATH", db_path), patch.object(
                config, "VALIDATION_AUTO_UPDATE_ENABLED", False
            ):
                app = create_app()
                response = app.test_client().get(
                    "/api/strategy-validation/daily?strategy=tomorrow_picks&date=2026-07-08"
                )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["summary"]["sample_count"], 0)
        self.assertEqual(payload["summary"]["pending_count"], 2)
        self.assertIsNone(payload["summary"]["win_rate"])
        self.assertIsNone(payload["summary"]["avg_return"])

    def test_strategy_validation_daily_auto_updates_pending_outcomes(self):
        import tempfile

        history = pd.DataFrame(
            [
                {"trade_date": "20260706", "open": 10.0, "high": 10.2, "low": 9.8, "price": 10.0},
                {"trade_date": "20260707", "open": 10.0, "high": 11.2, "low": 9.9, "price": 11.0},
            ]
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = "{}/validation.sqlite3".format(tmpdir)
            store = StrategyValidationStore(db_path)
            store.save_signals(
                "tomorrow_picks",
                "tomorrow_picks_v5",
                "2026-07-06T15:00:00",
                [{"rank": 1, "code": "600001", "name": "已可回填", "price": 10, "score": 80}],
            )
            with patch.object(config, "VALIDATION_DB_PATH", db_path), patch.object(
                config, "VALIDATION_AUTO_UPDATE_ENABLED", False
            ), patch(
                "stock_analyzer.app.MarketDataProvider.get_history",
                return_value=history,
            ), patch(
                "stock_analyzer.app.MarketDataProvider.get_realtime_quotes",
                return_value=pd.DataFrame(),
            ):
                app = create_app()
                response = app.test_client().get(
                    "/api/strategy-validation/daily?strategy=tomorrow_picks&date=2026-07-06&update=1"
                )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["update"]["updated"], 1)
        self.assertEqual(payload["summary"]["sample_count"], 1)
        self.assertEqual(payload["summary"]["up_count"], 1)
        self.assertEqual(payload["summary"]["down_count"], 0)
        self.assertGreater(payload["summary"]["win_rate"], 0)

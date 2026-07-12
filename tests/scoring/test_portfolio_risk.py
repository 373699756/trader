import json
import os
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from stock_analyzer import config
from stock_analyzer.app import create_app
from stock_analyzer.paper_trading import PaperTradingStore
from stock_analyzer.portfolio import build_portfolio


class PortfolioRiskTest(unittest.TestCase):
    def test_plan_defaults_keep_conservative_guards_enabled_and_model_takeover_off(self):
        self.assertTrue(config.ENABLE_SURVIVORSHIP_CORRECTION)
        self.assertTrue(config.ENABLE_TAIL_AUCTION_SLIPPAGE)
        self.assertTrue(config.ENABLE_MARKET_IMPACT)
        self.assertTrue(config.ENABLE_REGIME_THEME_CAP)
        self.assertTrue(config.ENABLE_STRESS_TEST)
        self.assertTrue(config.ENABLE_CALIBRATE_FDR)

        self.assertFalse(config.ENABLE_EXPECTED_RETURN_RANKING)
        self.assertFalse(config.ENABLE_INTERACTION_TERMS)
        self.assertFalse(config.ENABLE_REGIME_SPECIFIC_WEIGHTS)
        self.assertFalse(config.META_LABELING_ENFORCE_ACTION)
        self.assertFalse(config.ENABLE_EVENT_ALPHA)
        self.assertFalse(config.ENABLE_ENSEMBLE)

    class FakeValidationStore:
        def __init__(self, rows):
            self.rows = rows

        def list_signal_dates(self, strategy_name):
            return [{"strategy_name": strategy_name, "signal_date": "2024-01-01"}]

        def signals_for_date(self, signal_date, strategy_name):
            signals = []
            for row in self.rows:
                signals.append(
                    {
                        "rank": row.get("rank"),
                        "code": row.get("code"),
                        "name": row.get("name"),
                        "price_at_signal": row.get("price"),
                        "turnover": row.get("turnover"),
                        "theme": row.get("theme"),
                        "score": row.get("score"),
                        "raw": dict(row),
                    }
                )
            return signals

    class FakeAppContainer:
        provider = object()
        validation_store = object()

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

    def test_build_portfolio_excludes_backup_pool_when_tiers_exist(self):
        rows = [
            {
                "rank": 1,
                "code": "600001",
                "name": "备选A",
                "score": 60,
                "tier": "backup_pool",
                "theme": "半导体",
                "serenity_profile": {"confidence_score": 80, "risk_score": 40},
            },
            {
                "rank": 2,
                "code": "600002",
                "name": "备选B",
                "score": 58,
                "tier": "backup_pool",
                "theme": "算力",
                "serenity_profile": {"confidence_score": 80, "risk_score": 40},
            },
        ]

        result = build_portfolio(rows)

        self.assertEqual(result["rows"], [])
        self.assertEqual(result["summary"]["cash_pct"], 100.0)
        self.assertEqual(result["summary"]["excluded_count"], 2)
        self.assertIn("备选观察不参与组合分仓", result["summary"]["no_trade_reason"])

    def test_build_portfolio_keeps_primary_watch_when_tiers_exist(self):
        rows = [
            {
                "rank": 1,
                "code": "600001",
                "name": "重点",
                "score": 80,
                "tier": "primary_watch",
                "theme": "半导体",
                "serenity_profile": {"confidence_score": 80, "risk_score": 40},
            },
            {
                "rank": 2,
                "code": "600002",
                "name": "备选",
                "score": 60,
                "tier": "backup_pool",
                "theme": "算力",
                "serenity_profile": {"confidence_score": 80, "risk_score": 40},
            },
        ]

        result = build_portfolio(rows, market_regime={"score": 80})

        self.assertEqual([row["code"] for row in result["rows"]], ["600001"])
        self.assertEqual(result["summary"]["excluded_count"], 1)

    def test_build_portfolio_excludes_non_executable_rows_without_tiers(self):
        result = build_portfolio(
            [
                {
                    "rank": 1,
                    "code": "600001",
                    "score": 80,
                    "execution_allowed": False,
                    "serenity_profile": {"confidence_score": 80, "risk_score": 40},
                }
            ]
        )

        self.assertEqual(result["rows"], [])
        self.assertEqual(result["summary"]["cash_pct"], 100.0)

    def test_build_portfolio_filters_market_impact_overflow_and_backfills(self):
        rows = [
            {
                "rank": 1,
                "code": "600001",
                "name": "低流动",
                "score": 90,
                "turnover": 5_000_000,
                "serenity_profile": {"confidence_score": 90, "risk_score": 30},
            },
            {
                "rank": 2,
                "code": "600002",
                "name": "高流动A",
                "score": 88,
                "turnover": 1_000_000_000,
                "serenity_profile": {"confidence_score": 80, "risk_score": 40},
            },
            {
                "rank": 3,
                "code": "600003",
                "name": "高流动B",
                "score": 86,
                "turnover": 1_000_000_000,
                "serenity_profile": {"confidence_score": 75, "risk_score": 45},
            },
        ]

        with patch.object(config, "ENABLE_MARKET_IMPACT", True), patch.object(
            config, "VALIDATION_PORTFOLIO_CAPITAL", 10_000_000
        ), patch.object(config, "MARKET_IMPACT_COEFFICIENT", 0.1), patch.object(
            config, "MAX_ACCEPTABLE_IMPACT_PCT", 1.0
        ):
            result = build_portfolio(rows, max_positions=2, single_cap=0.5, theme_cap=1.0)

        self.assertEqual([row["code"] for row in result["rows"]], ["600002", "600003"])
        self.assertEqual(result["summary"]["capacity_excluded_count"], 1)
        self.assertFalse(result["summary"]["capacity_ok"])
        self.assertEqual(result["summary"]["capacity_overflow"][0]["code"], "600001")
        self.assertTrue(all(row["impact_pct"] <= 1.0 for row in result["rows"]))

    def test_stress_test_samples_reports_worst_scenario(self):
        from stock_analyzer.stress_scenarios import stress_test_samples

        samples = [
            {"signal_date": "2024-01-01", "primary_return_net": 1.0},
            {"signal_date": "2024-01-02", "primary_return_net": -3.0},
            {"signal_date": "2024-02-01", "primary_return_net": 0.5},
            {"signal_date": "2024-02-02", "primary_return_net": 0.8},
        ]
        scenarios = [
            {"name": "stress", "dates": [["2024-01-01", "2024-01-31"]]},
            {"name": "calm", "dates": [["2024-02-01", "2024-02-28"]]},
        ]

        result = stress_test_samples(samples, scenarios=scenarios)

        self.assertEqual(result["scenario_count"], 2)
        self.assertEqual(result["worst_scenario"]["scenario"], "stress")
        self.assertLess(result["worst_scenario"]["total_return"], 0)

    def test_stress_scenarios_load_from_json(self):
        from stock_analyzer.stress_scenarios import load_stress_scenarios

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "stress.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"scenarios": [{"name": "x", "dates": [["2024-01-01", "2024-01-02"]]}]}, handle)
            scenarios = load_stress_scenarios(path)

        self.assertEqual(scenarios[0]["name"], "x")

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

    def test_portfolio_volatility_targeting_reduces_gross_exposure(self):
        rows = [
            {
                "rank": idx + 1,
                "code": "60000{}".format(idx),
                "name": "高波动{}".format(idx),
                "theme": "主题{}".format(idx),
                "score": 90 - idx,
                "volatility_20d": 10.0,
                "serenity_profile": {"confidence_score": 80, "risk_score": 40},
            }
            for idx in range(4)
        ]

        with patch.object(config, "ENABLE_VOLATILITY_TARGETING", True), patch.object(
            config, "PORTFOLIO_TARGET_VOLATILITY_PCT", 5.0
        ), patch.object(config, "PORTFOLIO_VOL_SCALE_MIN", 0.35), patch.object(
            config, "PORTFOLIO_VOL_SCALE_MAX", 1.15
        ):
            result = build_portfolio(
                rows,
                max_positions=4,
                single_cap=0.4,
                theme_cap=0.7,
                market_regime={"score": 80, "level": "risk_on"},
            )

        self.assertAlmostEqual(result["summary"]["portfolio_volatility_pct"], 10.0)
        self.assertAlmostEqual(result["summary"]["volatility_factor"], 0.5)
        self.assertAlmostEqual(result["summary"]["total_weight"], 50.0, places=1)
        self.assertIn("组合降仓", " ".join(result["summary"]["gross_reasons"]))

    def test_portfolio_optimization_caps_correlation_group_exposure(self):
        rows = [
            {
                "rank": 1,
                "code": "600001",
                "name": "同组强A",
                "theme": "半导体",
                "correlation_group": "AI链",
                "score": 95,
                "expected_return_net": 2.0,
                "p_win": 0.65,
                "serenity_profile": {"confidence_score": 95, "risk_score": 25},
            },
            {
                "rank": 2,
                "code": "600002",
                "name": "同组强B",
                "theme": "算力",
                "correlation_group": "AI链",
                "score": 93,
                "expected_return_net": 1.8,
                "p_win": 0.63,
                "serenity_profile": {"confidence_score": 90, "risk_score": 28},
            },
            {
                "rank": 3,
                "code": "600003",
                "name": "独立A",
                "theme": "医药",
                "correlation_group": "医药",
                "score": 80,
                "expected_return_net": 0.8,
                "p_win": 0.56,
                "serenity_profile": {"confidence_score": 70, "risk_score": 45},
            },
            {
                "rank": 4,
                "code": "600004",
                "name": "独立B",
                "theme": "军工",
                "correlation_group": "军工",
                "score": 78,
                "expected_return_net": 0.6,
                "p_win": 0.54,
                "serenity_profile": {"confidence_score": 68, "risk_score": 48},
            },
        ]

        with patch.object(config, "ENABLE_PORTFOLIO_OPTIMIZATION", True), patch.object(
            config, "PORTFOLIO_CORRELATION_GROUP_CAP", 0.45
        ), patch.object(config, "ENABLE_VOLATILITY_TARGETING", False):
            result = build_portfolio(rows, max_positions=4, single_cap=0.4, theme_cap=0.8, market_regime={"score": 80})

        self.assertTrue(result["summary"]["portfolio_optimization_enabled"])
        self.assertLessEqual(result["summary"]["correlation_exposure"]["AI链"], 45.01)
        self.assertEqual(result["rows"][0]["portfolio_correlation_group"], "AI链")
        self.assertAlmostEqual(result["summary"]["correlation_group_cap_pct"], 45.0)

    def test_portfolio_optimization_expected_edge_affects_weights(self):
        rows = [
            {
                "rank": 1,
                "code": "600001",
                "name": "高边际",
                "theme": "半导体",
                "correlation_group": "半导体",
                "score": 80,
                "expected_return_net": 2.0,
                "p_win": 0.66,
                "model_confidence": "ready",
                "ranking_source": "expected_return_predicted_net_return",
                "expected_return_rank": 1,
                "serenity_profile": {"confidence_score": 70, "risk_score": 40},
            },
            {
                "rank": 2,
                "code": "600002",
                "name": "低边际",
                "theme": "医药",
                "correlation_group": "医药",
                "score": 80,
                "expected_return_net": -1.0,
                "p_win": 0.45,
                "model_confidence": "ready",
                "ranking_source": "expected_return_predicted_net_return",
                "expected_return_rank": 2,
                "serenity_profile": {"confidence_score": 70, "risk_score": 40},
            },
        ]

        with patch.object(config, "ENABLE_PORTFOLIO_OPTIMIZATION", True), patch.object(
            config, "ENABLE_VOLATILITY_TARGETING", False
        ):
            result = build_portfolio(rows, max_positions=2, single_cap=0.8, theme_cap=1.0, market_regime={"score": 80})

        weights = {row["code"]: row["suggested_weight"] for row in result["rows"]}
        self.assertGreater(weights["600001"], weights["600002"])

    def test_portfolio_optimization_ignores_low_confidence_expected_edge(self):
        rows = [
            {
                "rank": 1,
                "code": "600001",
                "name": "低置信高边际",
                "theme": "半导体",
                "correlation_group": "半导体",
                "score": 80,
                "expected_return_net": 2.0,
                "p_win": 0.66,
                "model_confidence": "low",
                "serenity_profile": {"confidence_score": 70, "risk_score": 40},
            },
            {
                "rank": 2,
                "code": "600002",
                "name": "低置信低边际",
                "theme": "医药",
                "correlation_group": "医药",
                "score": 80,
                "expected_return_net": -1.0,
                "p_win": 0.45,
                "model_confidence": "low",
                "serenity_profile": {"confidence_score": 70, "risk_score": 40},
            },
        ]

        with patch.object(config, "ENABLE_PORTFOLIO_OPTIMIZATION", True), patch.object(
            config, "ENABLE_VOLATILITY_TARGETING", False
        ):
            result = build_portfolio(rows, max_positions=2, single_cap=0.8, theme_cap=1.0, market_regime={"score": 80})

        weights = {row["code"]: row["suggested_weight"] for row in result["rows"]}
        self.assertAlmostEqual(weights["600001"], weights["600002"])

    def test_portfolio_optimization_ignores_score_bucket_probability_for_weights(self):
        rows = [
            {
                "rank": 1,
                "code": "600001",
                "name": "分桶高胜率",
                "theme": "半导体",
                "correlation_group": "半导体",
                "score": 80,
                "expected_return_net": 0.0,
                "calibrated_probability": 0.90,
                "model_confidence": "ready",
                "ranking_source": "expected_return_predicted_net_return",
                "expected_return_rank": 1,
                "serenity_profile": {"confidence_score": 70, "risk_score": 40},
            },
            {
                "rank": 2,
                "code": "600002",
                "name": "分桶低胜率",
                "theme": "医药",
                "correlation_group": "医药",
                "score": 80,
                "expected_return_net": 0.0,
                "calibrated_probability": 0.10,
                "model_confidence": "ready",
                "ranking_source": "expected_return_predicted_net_return",
                "expected_return_rank": 2,
                "serenity_profile": {"confidence_score": 70, "risk_score": 40},
            },
        ]

        with patch.object(config, "ENABLE_PORTFOLIO_OPTIMIZATION", True), patch.object(
            config, "ENABLE_VOLATILITY_TARGETING", False
        ):
            result = build_portfolio(rows, max_positions=2, single_cap=0.8, theme_cap=1.0, market_regime={"score": 80})

        weights = {row["code"]: row["suggested_weight"] for row in result["rows"]}
        self.assertAlmostEqual(weights["600001"], weights["600002"])

    def test_portfolio_theme_cap_tightens_in_risk_off_when_enabled(self):
        rows = [
            {
                "rank": idx + 1,
                "code": "60000{}".format(idx),
                "name": "样本{}".format(idx),
                "theme": "半导体" if idx < 3 else "医药",
                "score": 90 - idx,
                "serenity_profile": {"confidence_score": 80 - idx, "risk_score": 40 + idx},
            }
            for idx in range(5)
        ]

        with patch.object(config, "ENABLE_REGIME_THEME_CAP", True), patch.object(
            config, "PORTFOLIO_THEME_CAP_RISK_OFF_MULTIPLIER", 0.6
        ):
            result = build_portfolio(
                rows,
                max_positions=5,
                single_cap=0.2,
                theme_cap=0.5,
                market_regime={"level": "risk_off", "score": 35},
            )

        self.assertEqual(result["summary"]["base_theme_cap_pct"], 50.0)
        self.assertAlmostEqual(result["summary"]["theme_cap_pct"], 30.0)
        self.assertLessEqual(result["exposure"]["半导体"], 30.01)

    def test_paper_trading_store_records_closed_trades_and_nav(self):
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
            paper_path = "{}/paper.sqlite3".format(tmpdir)
            validation_store = self.FakeValidationStore(rows)
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
        self.assertAlmostEqual(trade["weighted_return_pct"], trade["net_return_pct"] / 5, places=4)

    def test_portfolio_endpoint_uses_latest_saved_snapshot(self):
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

        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "stock_analyzer.app.ApplicationContainer",
            return_value=self.FakeAppContainer(),
        ), patch("stock_analyzer.app.AppServices.start_background_workers", return_value=None):
            validation_path = "{}/validation.sqlite3".format(tmpdir)
            with patch.object(config, "VALIDATION_DB_PATH", validation_path), patch.object(
                config, "VALIDATION_AUTO_UPDATE_ENABLED", False
            ), patch.object(config, "PORTFOLIO_SINGLE_CAP", 0.4), patch.object(
                config, "PORTFOLIO_THEME_CAP", 0.7
            ):
                app = create_app()
                response = app.test_client().get("/api/portfolio?strategy=tomorrow_picks")

        self.assertEqual(response.status_code, 404)

    def test_portfolio_performance_endpoint_returns_paper_nav(self):
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
            paper_path = "{}/paper.sqlite3".format(tmpdir)
            validation_path = "{}/validation.sqlite3".format(tmpdir)
            validation_store = self.FakeValidationStore(rows)
            with patch.object(config, "PORTFOLIO_SINGLE_CAP", 1.0), patch.object(config, "PORTFOLIO_THEME_CAP", 1.0):
                PaperTradingStore(paper_path).run_paper_trade(FakeProvider(), validation_store, "tomorrow_picks")
            with patch.object(config, "VALIDATION_DB_PATH", validation_path), patch.object(
                config, "PAPER_TRADING_DB_PATH", paper_path
            ), patch.object(config, "VALIDATION_AUTO_UPDATE_ENABLED", False), patch(
                "stock_analyzer.app.ApplicationContainer",
                return_value=self.FakeAppContainer(),
            ), patch("stock_analyzer.app.AppServices.start_background_workers", return_value=None):
                app = create_app()
                client = app.test_client()
                perf_response = client.get("/api/portfolio/performance?strategy=tomorrow_picks&days=20")
                trades_response = client.get("/api/paper-trades?strategy=tomorrow_picks")
                portfolio_response = client.get("/api/portfolio?strategy=tomorrow_picks")

        self.assertEqual(perf_response.status_code, 404)
        self.assertEqual(trades_response.status_code, 404)
        self.assertEqual(portfolio_response.status_code, 404)

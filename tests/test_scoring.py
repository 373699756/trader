import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

from stock_analyzer import config
from stock_analyzer.backtest import run_alphalite_backtest, run_rolling_alphalite_backtest
from stock_analyzer.factors import compute_alphalite_for_stock
from stock_analyzer.history_cache import HistoryCache
from stock_analyzer.normalization import rename_known_columns
from stock_analyzer.providers import MarketDataProvider, _normalize_eastmoney_spot, _request_eastmoney_page
from stock_analyzer.scoring import (
    prepare_candidates,
    score_candidates,
    score_dual_horizon_candidates,
    score_position_candidates,
    score_swing_candidates,
    score_tech_potential_candidates,
    score_tomorrow_candidates,
)
from stock_analyzer.sentiment import score_news_items
from stock_analyzer.stability import TopKDropoutTracker
from stock_analyzer.strategy_validation import StrategyValidationStore


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

    def test_tomorrow_candidates_rank_buyable_liquid_names(self):
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


if __name__ == "__main__":
    unittest.main()

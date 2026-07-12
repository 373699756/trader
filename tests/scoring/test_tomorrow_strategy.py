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
    score_candidates,
    score_today_candidates,
    score_swing_candidates,
    score_tomorrow_candidates,
)
from stock_analyzer.sentiment import score_news_items
from stock_analyzer.stability import TopKDropoutTracker
from stock_analyzer.snapshot import _apply_close_anchor_prices, run_snapshot
from stock_analyzer.strategy_validation import StrategyValidationStore, _primary_return_config, validation_baseline_config
from stock_analyzer.validation_replay import backfill_strategy_validation_samples


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





class TomorrowStrategyTest(unittest.TestCase):

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
        self.assertIn("expected_return_net", row)
        self.assertIn("p_win", row)
        self.assertIn("downside_p10", row)
        self.assertIn("predicted_net_return", row)
        self.assertIsNone(row["predicted_net_return"])
        self.assertFalse(row["expected_return_available"])
        self.assertNotIn("rank_score", row)
        self.assertEqual(row["model_confidence"], "low")

    def test_tomorrow_consumes_oos_deepseek_rules_from_weights(self):
        quotes = pd.DataFrame(
            [
                {"code": "600001", "name": "会被规则扣分", "price": 10.6, "open": 10.1, "high": 10.8, "low": 10.0,
                 "pct_chg": 4.2, "turnover": 9e8, "turnover_rate": 5, "volume_ratio": 2.0,
                 "speed": 0.4, "sixty_day_pct": 18, "ytd_pct": 20, "amplitude": 5},
                {"code": "600002", "name": "规则未命中", "price": 10.3, "open": 10.1, "high": 10.5, "low": 10.0,
                 "pct_chg": 2.0, "turnover": 8e8, "turnover_rate": 4, "volume_ratio": 1.6,
                 "speed": 0.2, "sixty_day_pct": 8, "ytd_pct": 12, "amplitude": 4},
            ]
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "weights.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "deepseek_rules": {
                            "tomorrow_picks": [
                                {"field": "pct_chg", "operator": ">", "threshold": 3, "penalty": 40, "reason": "涨幅过热OOS验证弱"}
                            ]
                        }
                    },
                    handle,
                    ensure_ascii=False,
                )
            with patch.object(config, "WEIGHTS_OVERRIDE_PATH", path), patch.object(
                config,
                "DEEPSEEK_SHADOW_ONLY",
                False,
            ), patch(
                "stock_analyzer.scoring._tomorrow_display_gate",
                return_value=(2, 0.0, "测试展示全部候选"),
            ):
                rows, _ = score_tomorrow_candidates(prepare_candidates(quotes), top_n=2)

        self.assertEqual(rows[0]["code"], "600002")
        penalized = next(row for row in rows if row["code"] == "600001")
        self.assertEqual(penalized["deepseek_rule_penalty"], 40)
        self.assertEqual(penalized["score_before_deepseek_rules"], round(penalized["score"] + 40, 2))
        self.assertIn("deepseek_rules_matched", penalized)

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

        with patch("stock_analyzer.scoring._tomorrow_intraday_relaxed_mode", return_value=False):
            rows, meta = score_tomorrow_candidates(
                prepare_candidates(quotes),
                top_n=36,
                market_regime={"level": "risk_off", "label": "偏防守", "score": 38},
                display_cap=0,
            )

        self.assertLessEqual(len(rows), 18)
        self.assertGreater(len(rows), 0)
        self.assertTrue(all(row["score"] >= meta["display_min_score"] for row in rows))
        self.assertEqual(meta["display_limit"], 36)
        self.assertIn("不足则不推荐", meta["gate_reason"])
        self.assertIn(rows[0]["tier"], {"primary_watch", "backup_pool"})

    def test_tomorrow_default_display_cap_can_be_overridden_for_snapshots(self):
        quotes = pd.DataFrame(
            [
                {
                    "code": "600{:03d}".format(index),
                    "name": "展示样本{}".format(index),
                    "price": 10 + index * 0.1,
                    "pct_chg": 2.0 + (index % 4) * 0.2,
                    "turnover": 500000000 + index * 10000000,
                    "turnover_rate": 4,
                    "volume_ratio": 1.5,
                    "speed": 0.2,
                    "sixty_day_pct": 12,
                    "ytd_pct": 18,
                    "amplitude": 4,
                    "industry": "行业{}".format(index),
                }
                for index in range(20)
            ]
        )
        candidates = prepare_candidates(quotes)

        with patch("stock_analyzer.scoring._tomorrow_display_gate", return_value=(20, 0.0, "测试展示全部候选")):
            display_rows, display_meta = score_tomorrow_candidates(candidates, top_n=20)
            snapshot_rows, snapshot_meta = score_tomorrow_candidates(candidates, top_n=20, display_cap=0)

        self.assertLessEqual(len(display_rows), config.TOMORROW_RECOMMENDATION_DISPLAY_LIMIT)
        self.assertEqual(display_meta["display_limit"], config.TOMORROW_RECOMMENDATION_DISPLAY_LIMIT)
        self.assertGreater(len(snapshot_rows), len(display_rows))
        self.assertEqual(snapshot_meta["display_limit"], 20)
        self.assertEqual(snapshot_meta["display_cap"], 0)

    def test_tomorrow_pre_1430_relaxes_hard_reject_thresholds(self):
        quotes = pd.DataFrame(
            [
                {
                    "code": "600001",
                    "name": "早盘低换手样本",
                    "price": 10.5,
                    "open": 10.1,
                    "high": 10.7,
                    "low": 10.0,
                    "pct_chg": 3.2,
                    "turnover": 8e8,
                    "turnover_rate": 1.0,
                    "volume_ratio": 1.2,
                    "speed": 0.4,
                    "sixty_day_pct": 18,
                    "ytd_pct": 24,
                    "amplitude": 4.8,
                },
                {
                    "code": "600002",
                    "name": "对照样本",
                    "price": 10.3,
                    "open": 10.1,
                    "high": 10.4,
                    "low": 10.0,
                    "pct_chg": 2.0,
                    "turnover": 7e8,
                    "turnover_rate": 3.0,
                    "volume_ratio": 1.8,
                    "speed": 0.2,
                    "sixty_day_pct": 10,
                    "ytd_pct": 14,
                    "amplitude": 3.5,
                },
            ]
        )
        candidates = prepare_candidates(quotes)

        with patch("stock_analyzer.scoring._tomorrow_intraday_relaxed_mode", return_value=False), patch(
            "stock_analyzer.scoring._tomorrow_display_gate",
            return_value=(10, 0.0, "测试展示全部候选"),
        ):
            strict_rows, _ = score_tomorrow_candidates(candidates, top_n=10)
        with patch("stock_analyzer.scoring._tomorrow_intraday_relaxed_mode", return_value=True), patch(
            "stock_analyzer.scoring._tomorrow_display_gate",
            return_value=(10, 0.0, "测试展示全部候选"),
        ):
            relaxed_rows, meta = score_tomorrow_candidates(candidates, top_n=10)

        self.assertEqual([row["code"] for row in strict_rows], ["600002"])
        self.assertEqual([row["code"] for row in relaxed_rows], ["600001", "600002"])
        self.assertTrue(meta["intraday_relaxed_mode"])
        self.assertEqual(meta["primary_watch_count"], 0)
        self.assertEqual(meta["backup_watch_count"], 2)
        self.assertEqual(meta["provisional_mode"], "intraday_watch")
        self.assertTrue(all(row["tier_label"] == "盘中观察" for row in relaxed_rows))
        self.assertTrue(all(row["trade_action"]["position_size"] == 0 for row in relaxed_rows))
        self.assertTrue(all(row["agent_committee"]["stance"] == "wait" for row in relaxed_rows))
        self.assertTrue(all(row["agent_committee"]["final_action_label"] == "盘中观察" for row in relaxed_rows))
        self.assertTrue(all(row["verdict"]["label"] == "盘中观察" for row in relaxed_rows))

    def test_tomorrow_intraday_mode_only_runs_during_weekday_market_window(self):
        from stock_analyzer.scoring import _tomorrow_intraday_relaxed_mode

        self.assertFalse(_tomorrow_intraday_relaxed_mode(datetime(2026, 7, 10, 9, 29)))
        self.assertTrue(_tomorrow_intraday_relaxed_mode(datetime(2026, 7, 10, 9, 30)))
        self.assertTrue(_tomorrow_intraday_relaxed_mode(datetime(2026, 7, 10, 12, 0)))
        self.assertFalse(_tomorrow_intraday_relaxed_mode(datetime(2026, 7, 10, 14, 30)))
        self.assertFalse(_tomorrow_intraday_relaxed_mode(datetime(2026, 7, 11, 10, 0)))
        self.assertFalse(
            _tomorrow_intraday_relaxed_mode(
                datetime(2026, 7, 10, 10, 0),
                quote_time=datetime(2026, 7, 9, 15, 0),
            )
        )

    def test_tomorrow_intraday_mode_does_not_relax_overheat_ceiling(self):
        quotes = pd.DataFrame(
            [
                {
                    "code": "600001",
                    "name": "量比过热",
                    "price": 10.5,
                    "open": 10.1,
                    "high": 10.7,
                    "low": 10.0,
                    "pct_chg": 3.2,
                    "turnover": 8e8,
                    "turnover_rate": 3.0,
                    "volume_ratio": 6.2,
                    "speed": 0.4,
                    "sixty_day_pct": 18,
                    "ytd_pct": 24,
                    "amplitude": 4.8,
                }
            ]
        )

        with patch("stock_analyzer.scoring._tomorrow_intraday_relaxed_mode", return_value=True), patch(
            "stock_analyzer.scoring._tomorrow_display_gate",
            return_value=(10, 0.0, "测试展示全部候选"),
        ):
            rows, _ = score_tomorrow_candidates(prepare_candidates(quotes), top_n=10)

        self.assertEqual(rows, [])

    def test_tomorrow_display_gate_relaxes_before_1430(self):
        from stock_analyzer.scoring import _tomorrow_display_gate

        regime = {"level": "risk_on", "label": "偏进攻", "score": 85}

        _, strict_min_score, strict_reason = _tomorrow_display_gate(18, regime, intraday_relaxed=False)
        _, relaxed_min_score, relaxed_reason = _tomorrow_display_gate(18, regime, intraday_relaxed=True)

        self.assertEqual(strict_min_score, 60.0)
        self.assertEqual(relaxed_min_score, 56.0)
        self.assertIn("14:30 前早盘模式", relaxed_reason)
        self.assertNotIn("14:30 前早盘模式", strict_reason)

    def test_tomorrow_mid_gain_weak_close_penalty_marks_row(self):
        quotes = pd.DataFrame(
            [
                {
                    "code": "600001",
                    "name": "弱收盘",
                    "price": 10.4,
                    "open": 10.1,
                    "high": 11.0,
                    "low": 10.0,
                    "pct_chg": 5.2,
                    "turnover": 600000000,
                    "turnover_rate": 2.5,
                    "volume_ratio": 1.6,
                    "speed": 0.2,
                    "sixty_day_pct": 4,
                    "ytd_pct": 8,
                    "amplitude": 6,
                },
                {
                    "code": "600002",
                    "name": "强收盘",
                    "price": 10.85,
                    "open": 10.1,
                    "high": 11.0,
                    "low": 10.0,
                    "pct_chg": 5.2,
                    "turnover": 600000000,
                    "turnover_rate": 2.5,
                    "volume_ratio": 1.6,
                    "speed": 0.2,
                    "sixty_day_pct": 4,
                    "ytd_pct": 8,
                    "amplitude": 6,
                },
            ]
        )

        with patch("stock_analyzer.scoring._tomorrow_intraday_relaxed_mode", return_value=False), patch(
            "stock_analyzer.scoring._tomorrow_display_gate", return_value=(2, 0.0, "测试展示全部候选")
        ):
            rows, _ = score_tomorrow_candidates(prepare_candidates(quotes), top_n=2, display_cap=0)

        weak = next(row for row in rows if row["code"] == "600001")
        strong = next(row for row in rows if row["code"] == "600002")
        self.assertGreater(weak["risk_penalty_parts"]["mid_gain_weak_close"], 0)
        self.assertLessEqual(weak["risk_penalty_parts"]["mid_gain_weak_close"], config.TOMORROW_MID_GAIN_WEAK_CLOSE_PENALTY)
        self.assertTrue(weak["mid_gain_weak_close_flag"])
        self.assertIn("4-7%涨幅且尾盘承接不足", weak["reasons"])
        self.assertNotIn("mid_gain_weak_close", strong["risk_penalty_parts"])

    def test_tomorrow_tail_score_penalizes_extremely_weak_close_more(self):
        from stock_analyzer.scoring import _tail_close_setup_score

        base = {
            "pct_chg": 2.0,
            "open": 10.0,
            "high": 11.0,
            "low": 10.0,
            "amplitude": 5.0,
            "volume_ratio": 1.5,
            "turnover_rate": 3.0,
            "speed": 0.2,
            "market": "main",
        }
        extremely_weak = _tail_close_setup_score(pd.Series({**base, "price": 10.2}))
        weak = _tail_close_setup_score(pd.Series({**base, "price": 10.4}))

        self.assertLess(extremely_weak, weak)

    def test_tomorrow_risk_off_can_return_empty_when_no_strict_match(self):
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

        self.assertGreater(len(rows), 0)
        self.assertTrue(all(row["tier"] == "backup_pool" for row in rows))
        self.assertEqual(meta["primary_watch_count"], 0)
        self.assertIn("测试严格门控", meta["gate_reason"])
        self.assertEqual(meta["fallback_mode"], "backup_pool")

    def test_tomorrow_can_return_empty_when_strict_filter_rejects_everything(self):
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

        with patch("stock_analyzer.scoring._tomorrow_intraday_relaxed_mode", return_value=False):
            rows, meta = score_tomorrow_candidates(
                prepare_candidates(quotes),
                top_n=36,
                market_regime={"level": "risk_off", "label": "偏防守", "score": 25},
            )

        self.assertGreater(len(rows), 0)
        self.assertTrue(all(row["tier"] == "backup_pool" for row in rows))
        self.assertEqual(meta["primary_watch_count"], 0)
        self.assertIn("不足则不推荐", meta["gate_reason"])
        self.assertEqual(meta["fallback_mode"], "backup_pool")
        self.assertIn("降级显示备选观察", meta["gate_reason"])

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

        with patch("stock_analyzer.scoring._tomorrow_intraday_relaxed_mode", return_value=False):
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

        with patch("stock_analyzer.scoring._tomorrow_intraday_relaxed_mode", return_value=False):
            rows, meta = score_tomorrow_candidates(
                prepare_candidates(quotes),
                top_n=36,
                market_regime={"level": "risk_on", "label": "偏进攻", "score": 75},
            )

        self.assertFalse(any(row["code"] == "300274" for row in rows))
        self.assertEqual(meta["primary_watch_count"], 1)
        self.assertEqual(meta["display_count"], 1)

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

        with patch("stock_analyzer.scoring._tomorrow_intraday_relaxed_mode", return_value=False):
            rows, meta = score_tomorrow_candidates(
                prepare_candidates(quotes),
                top_n=4,
                market_regime={"level": "risk_on", "label": "偏进攻", "score": 75},
            )

        primary_rows = [row for row in rows if row["tier"] == "primary_watch"]
        self.assertLessEqual(len(primary_rows), config.TOMORROW_MAX_PRIMARY_PER_THEME)
        self.assertGreaterEqual(meta["theme_limited_count"], 1)
        self.assertTrue(any("同主题重点观察已达上限" in row["reasons"] for row in rows))

    def test_tomorrow_display_caps_inferred_theme_when_industry_missing(self):
        quotes = pd.DataFrame(
            [
                {
                    "code": "688{:03d}".format(index),
                    "name": "芯片样本{}".format(index),
                    "price": 10 + index * 0.1,
                    "pct_chg": 2.0,
                    "turnover": 900000000 + index * 10000000,
                    "turnover_rate": 4,
                    "volume_ratio": 1.5,
                    "speed": 0.2,
                    "sixty_day_pct": 12,
                    "ytd_pct": 18,
                    "amplitude": 4,
                    "industry": "",
                    "ret_5d": 2,
                    "ret_10d": 4,
                    "ret_20d": 6,
                    "ma20_gap": 2,
                    "vol_amount_5d": 1.2,
                    "volatility_20d": 2,
                    "alphalite_factor_ready": 1,
                }
                for index in range(8)
            ]
        )

        rows, meta = score_tomorrow_candidates(
            prepare_candidates(quotes),
            top_n=8,
            market_regime={"level": "risk_on", "label": "偏进攻", "score": 75},
        )

        self.assertLessEqual(meta["theme_distribution"].get("半导体", 0), config.TOMORROW_MAX_DISPLAY_PER_THEME)
        self.assertGreaterEqual(meta["display_theme_limited_count"], 1)

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

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tier"], "backup_pool")
        self.assertEqual(rows[0]["trade_action"]["action"], "watch_only")
        self.assertEqual(rows[0]["trade_action"]["position_size"], 0.0)
        self.assertFalse(rows[0]["execution_allowed"])
        self.assertEqual(meta["primary_watch_count"], 0)
        self.assertEqual(meta["fallback_mode"], "backup_pool")
        self.assertIn("降级显示备选观察", meta["gate_reason"])

    def test_tomorrow_backup_applies_oos_rule_penalty(self):
        quotes = pd.DataFrame(
            [
                {
                    "code": "600001",
                    "name": "备选规则样本",
                    "price": 10,
                    "pct_chg": -0.8,
                    "turnover": 180000000,
                    "turnover_rate": 2,
                    "volume_ratio": 1.0,
                    "speed": -0.2,
                    "sixty_day_pct": 4,
                    "ytd_pct": 6,
                    "amplitude": 3,
                }
            ]
        )

        def apply_test_rule(strategy, row):
            item = dict(row)
            item["score_before_deepseek_rules"] = item["score"]
            item["deepseek_rule_penalty"] = 7.0
            item["score"] = 99.0
            return item

        with patch("stock_analyzer.scoring._tomorrow_intraday_relaxed_mode", return_value=False), patch(
            "stock_analyzer.scoring.apply_rule_penalty", side_effect=apply_test_rule
        ) as apply_rule:
            rows, meta = score_tomorrow_candidates(
                prepare_candidates(quotes),
                top_n=10,
                market_regime={"level": "risk_off", "label": "偏防守", "score": 30},
            )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["deepseek_rule_penalty"], 7.0)
        self.assertEqual(meta["fallback_mode"], "backup_pool")
        apply_rule.assert_called_once()

    def test_tomorrow_validation_gate_waits_for_enough_real_days(self):
        from stock_analyzer.app_support import tomorrow_validation_gate_decision

        decision = tomorrow_validation_gate_decision(
            {
                "sample_count": 2,
                "outcome_sample_count": 2,
                "real_sample_count": 2,
                "real_day_count": 2,
                "avg_primary_return_net": -1.0,
                "win_rate_primary_net": 0.0,
                "real_avg_primary_return_net": -1.0,
                "real_win_rate_primary_net": 0.0,
            }
        )

        self.assertTrue(decision["blocked"])
        self.assertEqual(decision["state"], "pending")
        self.assertIn("真实验证不足", decision["reason"])

    def test_tomorrow_validation_gate_ignores_bad_replay_metrics_when_real_is_good(self):
        from stock_analyzer.app_support import tomorrow_validation_gate_decision

        decision = tomorrow_validation_gate_decision(
            {
                "strategy_name": "tomorrow_picks",
                "sample_count": 80,
                "day_count": 40,
                "real_sample_count": 40,
                "real_day_count": 60,
                "avg_primary_return_net": -2.0,
                "win_rate_primary_net": 10.0,
                "real_avg_primary_return_net": 0.6,
                "real_win_rate_primary_net": 55.0,
                "avg_max_drawdown_3d": -1.0,
            }
        )

        self.assertFalse(decision["blocked"])
        self.assertEqual(decision["state"], "active")
        self.assertEqual(decision["position_scale"], 1.0)

    def test_tomorrow_validation_gate_demotes_primary_when_retired(self):
        from stock_analyzer.app import _apply_tomorrow_validation_gate

        rows = [
            {"code": "600001", "tier": "primary_watch", "tier_label": "重点观察", "reasons": []},
            {"code": "600002", "tier": "backup_pool", "tier_label": "备选观察", "reasons": []},
        ]
        meta = {"primary_watch_count": 1, "primary_gate_count": 1, "gate_reason": "原始门控"}

        decision = _apply_tomorrow_validation_gate(
            rows,
            meta,
            {
                "sample_count": 3,
                "outcome_sample_count": 3,
                "total_outcome_sample_count": 30,
                "real_sample_count": 60,
                "real_day_count": 60,
                "avg_primary_return_net": -0.8,
                "win_rate_primary_net": 30.0,
                "real_avg_primary_return_net": -0.4,
                "real_win_rate_primary_net": 40.0,
            },
        )

        self.assertTrue(decision["blocked"])
        self.assertTrue(decision["allows_backup"])
        self.assertEqual(meta["primary_watch_count"], 0)
        self.assertEqual(meta["backup_watch_count"], 2)
        self.assertEqual({row["tier"] for row in rows}, {"backup_pool"})
        self.assertTrue(all(row["trade_action"]["position_size"] == 0 for row in rows))
        self.assertTrue(all(not row["execution_allowed"] for row in rows))
        self.assertIn("验证退场", rows[0]["reasons"][0])
        self.assertIn("允许备选观察", decision["reason"])

    def test_tomorrow_validation_gate_preserves_intraday_watch_label(self):
        from stock_analyzer.app import _apply_tomorrow_validation_gate

        rows = [
            {
                "code": "600001",
                "tier": "backup_pool",
                "tier_label": "盘中观察",
                "observation_mode": "intraday_provisional",
                "reasons": [],
                "deepseek_action": "priority",
            }
        ]
        meta = {"primary_watch_count": 0, "primary_gate_count": 0}

        _apply_tomorrow_validation_gate(
            rows,
            meta,
            {
                "strategy_name": "tomorrow_picks",
                "sample_count": 20,
                "day_count": 20,
                "real_sample_count": 20,
                "real_day_count": 60,
                "real_avg_primary_return_net": -1.0,
                "real_win_rate_primary_net": 20.0,
            },
        )

        self.assertEqual(rows[0]["tier_label"], "盘中观察")
        self.assertEqual(rows[0]["trade_action"]["position_size"], 0.0)
        self.assertEqual(rows[0]["deepseek_action"], "watch")

    def test_tomorrow_validation_gate_demotes_primary_without_outcomes(self):
        from stock_analyzer.app import _apply_tomorrow_validation_gate

        rows = [{"code": "600001", "tier": "primary_watch", "tier_label": "重点观察", "reasons": []}]
        meta = {"primary_watch_count": 1, "primary_gate_count": 1}

        decision = _apply_tomorrow_validation_gate(rows, meta, {})

        self.assertTrue(decision["blocked"])
        self.assertEqual(rows[0]["tier"], "backup_pool")
        self.assertEqual(meta["primary_watch_count"], 0)
        self.assertEqual(rows[0]["trade_action"]["position_size"], 0.0)

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

    def test_tomorrow_objective_uses_sortino_when_return_series_available(self):
        from stock_analyzer.calibrate import _objective

        stable = {
            "absolute_win_rate": 60.0,
            "absolute_avg_period_return": 1.0,
            "absolute_median_period_return": 0.8,
            "absolute_loss_quantile_return": -0.5,
            "absolute_avg_next_open_return": 0.1,
            "absolute_avg_max_drawdown": -1.0,
            "absolute_max_drawdown": -2.0,
            "return_series": [1.2, 1.0, -0.4, -0.6],
        }
        unstable = {
            **stable,
            "absolute_loss_quantile_return": -3.0,
            "absolute_avg_max_drawdown": -3.0,
            "absolute_max_drawdown": -8.0,
            "return_series": [4.5, 3.5, -5.0, -0.2],
        }

        with patch.object(config, "CALIBRATE_USE_SORTINO", True):
            stable_obj = _objective(stable, "tomorrow_picks")
            unstable_obj = _objective(unstable, "tomorrow_picks")

        self.assertGreater(stable_obj, unstable_obj)

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
                config.TOMORROW_STRATEGY_VERSION,
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
        self.assertEqual(metrics["real_sample_count"], 1)
        self.assertEqual(metrics["real_total_sample_count"], 2)
        self.assertEqual(metrics["real_backup_sample_count"], 1)
        self.assertEqual(metrics["real_day_count"], 1)
        self.assertEqual(metrics["real_avg_primary_return_net"], metrics["avg_primary_return_net"])
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["code"], "600001")

    def test_tomorrow_validation_metrics_only_use_current_formal_version(self):
        histories = {
            "600001": _validation_history("2024-01-01", future_days=3, final_price=9.5),
            "600002": _validation_history("2024-01-02", future_days=3, final_price=10.8),
            "600003": _validation_history("2024-01-03", future_days=3, final_price=11.0),
        }

        class FakeProvider:
            def get_history(self, code, days=180):
                return histories[code]

        with tempfile.TemporaryDirectory() as tmpdir:
            store = StrategyValidationStore("{}/validation.sqlite3".format(tmpdir))
            store.save_signals(
                "tomorrow_picks",
                "tomorrow_picks_v5",
                "2024-01-01T15:00:00",
                [
                    {
                        "rank": 1,
                        "code": "600001",
                        "name": "旧版本",
                        "price": 10,
                        "score": 90,
                        "tier": "primary_watch",
                    }
                ],
            )
            store.save_signals(
                "tomorrow_picks",
                config.TOMORROW_STRATEGY_VERSION,
                "2024-01-02T15:00:00",
                [
                    {
                        "rank": 1,
                        "code": "600002",
                        "name": "当前版本",
                        "price": 10,
                        "score": 90,
                        "tier": "primary_watch",
                    }
                ],
            )
            store.save_signals(
                "tomorrow_picks",
                "tomorrow_picks_replay_v1",
                "2024-01-03T15:00:00",
                [
                    {
                        "rank": 1,
                        "code": "600003",
                        "name": "旧回放版本",
                        "price": 10,
                        "score": 95,
                        "tier": "primary_watch",
                    }
                ],
            )
            store.update_outcomes(FakeProvider(), strategy_name="tomorrow_picks")
            metrics = store.metrics("tomorrow_picks", days=20)
            samples = store.live_weight_samples("tomorrow_picks", days=20)

        self.assertEqual(metrics["strategy_version"], config.TOMORROW_STRATEGY_VERSION)
        self.assertEqual(metrics["real_sample_count"], 1)
        self.assertEqual(metrics["replay_sample_count"], 0)
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["code"], "600002")

    def test_tomorrow_pending_counts_only_use_current_formal_version(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StrategyValidationStore("{}/validation.sqlite3".format(tmpdir))
            store.save_signals(
                "tomorrow_picks",
                "tomorrow_picks_v5",
                "2024-01-01T15:00:00",
                [{"rank": 1, "code": "600001", "name": "旧版本", "price": 10, "score": 90}],
            )
            store.save_signals(
                "tomorrow_picks",
                config.TOMORROW_STRATEGY_VERSION,
                "2024-01-02T15:00:00",
                [{"rank": 1, "code": "600002", "name": "当前版本", "price": 10, "score": 90}],
            )

            metrics = store.metrics("tomorrow_picks", days=20)

        self.assertEqual(metrics["signal_sample_count"], 1)
        self.assertEqual(metrics["pending_outcome_count"], 1)

    def test_tomorrow_primary_return_config_uses_next_open_to_close(self):
        column, days, horizon = _primary_return_config("tomorrow_picks")
        self.assertEqual(column, "next_close_return")
        self.assertEqual(days, 1)
        self.assertEqual(horizon, "次日开盘至收盘")
        with patch.object(config, "VALIDATION_PRIMARY_ENTRY_MODE", "signal"):
            column_signal, days_signal, horizon_signal = _primary_return_config("tomorrow_picks")
            self.assertEqual(column_signal, "next_close_return")
            self.assertEqual(days_signal, 1)
            self.assertEqual(horizon_signal, "次日开盘至收盘")





if __name__ == '__main__':

    unittest.main()

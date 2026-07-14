import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

from stock_analyzer import config


class ModelsCalibrationTest(unittest.TestCase):
    def test_ranking_gate_score_keeps_strategy_score_after_expected_return_promotion(self):
        from stock_analyzer.scoring_core.expected_return import _expected_return_rank_active, _ranking_gate_score

        promoted = {
            "score": 50,
            "predicted_net_return": 2.4,
            "model_confidence": "ready",
            "ranking_source": "expected_return_predicted_net_return",
        }
        shadow = {
            "score": 50,
            "predicted_net_return": 2.4,
            "model_confidence": "ready",
        }
        low_confidence = {
            "score": 50,
            "predicted_net_return": 2.4,
            "model_confidence": "low",
            "ranking_source": "expected_return_predicted_net_return",
        }

        self.assertTrue(_expected_return_rank_active(promoted))
        self.assertEqual(_ranking_gate_score(promoted), 50)
        self.assertEqual(_ranking_gate_score(shadow), 50)
        self.assertEqual(_ranking_gate_score(low_confidence), 50)

    def test_expected_return_model_adds_shadow_prediction_fields_without_reordering(self):
        from stock_analyzer.expected_return_model import predict_expected_return

        rows = [
            {"code": "A", "score": 80, "risk_penalty": 2, "liquidity_score": 85, "momentum_score": 82},
            {"code": "B", "score": 60, "risk_penalty": 4, "liquidity_score": 45, "momentum_score": 40},
        ]
        samples = []
        for idx in range(25):
            high_score = idx < 15
            samples.append(
                {
                    "signal_date": "2024-01-{:02d}".format((idx % 10) + 1),
                    "stored_score": 78 if high_score else 62,
                    "primary_return_net": 1.0 if high_score else -0.5,
                    "max_drawdown": -1.0 if high_score else -3.0,
                    "raw": {
                        "score": 78 if high_score else 62,
                        "risk_penalty": 2 if high_score else 4,
                        "liquidity_score": 85 if high_score else 45,
                        "momentum_score": 82 if high_score else 40,
                    },
                }
            )

        enriched = predict_expected_return("tomorrow_picks", rows, samples=samples)

        self.assertEqual([row["code"] for row in enriched], ["A", "B"])
        self.assertIn(enriched[0]["model_confidence"], {"shadow", "ready"})
        self.assertNotIn("rank_score", enriched[0])
        self.assertGreater(enriched[0]["predicted_net_return"], enriched[1]["predicted_net_return"])
        self.assertGreater(enriched[0]["expected_return_sample_count"], 0)

    def test_expected_return_model_uses_component_feature_neighbors(self):
        from stock_analyzer.expected_return_model import predict_expected_return

        rows = [
            {
                "code": "A",
                "score": 70,
                "risk_penalty": 2,
                "liquidity_score": 88,
                "momentum_score": 86,
                "execution_score": 82,
                "tail_setup_score": 84,
            },
            {
                "code": "B",
                "score": 70,
                "risk_penalty": 2,
                "liquidity_score": 35,
                "momentum_score": 32,
                "execution_score": 40,
                "tail_setup_score": 38,
            },
        ]
        samples = []
        for idx in range(24):
            winner = idx < 12
            raw = {
                "score": 70,
                "risk_penalty": 2,
                "liquidity_score": 88 if winner else 35,
                "momentum_score": 86 if winner else 32,
                "execution_score": 82 if winner else 40,
                "tail_setup_score": 84 if winner else 38,
            }
            samples.append(
                {
                    "signal_date": "2024-02-{:02d}".format((idx % 12) + 1),
                    "primary_return_net": 2.0 if winner else -1.2,
                    "max_drawdown": -0.8 if winner else -4.0,
                    "raw": raw,
                }
            )

        enriched = predict_expected_return("tomorrow_picks", rows, samples=samples)

        self.assertEqual(enriched[0]["expected_return_peer_method"], "feature_nearest")
        self.assertEqual(enriched[1]["expected_return_peer_method"], "feature_nearest")
        self.assertGreater(enriched[0]["expected_return_net"], enriched[1]["expected_return_net"])
        self.assertGreater(enriched[0]["predicted_net_return"], enriched[1]["predicted_net_return"])

    def test_expected_return_model_time_decays_peer_outcomes(self):
        from stock_analyzer.expected_return_model import predict_expected_return

        rows = [
            {
                "code": "A",
                "score": 70,
                "risk_penalty": 2,
                "liquidity_score": 80,
                "momentum_score": 80,
                "execution_score": 80,
            }
        ]
        samples = []
        for day in range(1, 21):
            samples.append(
                {
                    "signal_date": "2024-01-{:02d}".format(day),
                    "primary_return_net": -1.0 if day <= 10 else 2.0,
                    "max_drawdown": -2.0,
                    "raw": {
                        "score": 70,
                        "risk_penalty": 2,
                        "liquidity_score": 80,
                        "momentum_score": 80,
                        "execution_score": 80,
                    },
                }
            )

        with patch.object(config, "CALIBRATE_TIME_DECAY_HALF_LIFE", 3):
            enriched = predict_expected_return("tomorrow_picks", rows, samples=samples)

        self.assertGreater(enriched[0]["expected_return_net"], 0.5)
        self.assertEqual(enriched[0]["expected_return_time_decay_half_life"], 3)

    def test_expected_return_model_penalizes_uncertain_peer_returns(self):
        from stock_analyzer.expected_return_model import predict_expected_return

        rows = [
            {
                "code": "STABLE",
                "score": 70,
                "risk_penalty": 2,
                "liquidity_score": 85,
                "momentum_score": 85,
                "execution_score": 85,
            },
            {
                "code": "VOL",
                "score": 70,
                "risk_penalty": 2,
                "liquidity_score": 35,
                "momentum_score": 35,
                "execution_score": 35,
            },
        ]
        samples = []
        for idx in range(20):
            samples.append(
                {
                    "signal_date": "2024-03-{:02d}".format(idx + 1),
                    "primary_return_net": 1.0,
                    "max_drawdown": -1.0,
                    "raw": {
                        "score": 70,
                        "risk_penalty": 2,
                        "liquidity_score": 85,
                        "momentum_score": 85,
                        "execution_score": 85,
                    },
                }
            )
            samples.append(
                {
                    "signal_date": "2024-03-{:02d}".format(idx + 1),
                    "primary_return_net": 4.0 if idx % 2 == 0 else -2.0,
                    "max_drawdown": -4.0,
                    "raw": {
                        "score": 70,
                        "risk_penalty": 2,
                        "liquidity_score": 35,
                        "momentum_score": 35,
                        "execution_score": 35,
                    },
                }
            )

        enriched = predict_expected_return("tomorrow_picks", rows, samples=samples)
        by_code = {row["code"]: row for row in enriched}

        self.assertGreater(
            by_code["VOL"]["expected_return_uncertainty"],
            by_code["STABLE"]["expected_return_uncertainty"],
        )
        self.assertTrue(by_code["VOL"]["expected_return_available"])
        self.assertNotIn("rank_score", by_code["VOL"])

    def test_expected_return_model_does_not_heuristic_fallback_without_feature_peers(self):
        from stock_analyzer.expected_return_model import predict_expected_return

        rows = [{"code": "A", "score": 95, "risk_penalty": 1}]
        samples = [
            {
                "signal_date": "2024-01-{:02d}".format((idx % 5) + 1),
                "stored_score": 90,
                "primary_return_net": 3.0,
                "raw": {"score": 90, "risk_penalty": 1},
            }
            for idx in range(25)
        ]

        enriched = predict_expected_return("tomorrow_picks", rows, samples=samples)

        self.assertFalse(enriched[0]["expected_return_available"])
        self.assertIsNone(enriched[0]["predicted_net_return"])
        self.assertIsNone(enriched[0]["p_win"])
        self.assertEqual(enriched[0]["expected_return_peer_method"], "insufficient_feature_peers")
        self.assertNotIn("rank_score", enriched[0])

    def test_expected_return_artifact_roundtrip_baseline_and_gate_guards(self):
        from stock_analyzer.expected_return_model import (
            build_expected_return_artifact,
            expected_return_artifact_promotion_gate,
            load_expected_return_artifact,
            save_expected_return_artifact,
        )

        samples = [
            {
                "signal_date": "2024-01-{:02d}".format((idx % 2) + 1),
                "stored_score": 80,
                "primary_return_net": 1.0,
                "max_drawdown": -1.0,
                "raw": {"score": 80, "risk_penalty": 1},
            }
            for idx in range(20)
        ]
        oos_result = {
            "ok": True,
            "status": "oos_passed",
            "baseline_oos_objective": 0.1,
            "rank_score_oos_objective": 0.25,
            "positive_folds": 3,
            "fold_count": 3,
            "margin": 0.05,
            "fdr": {"passed": True},
            "ci": {"passed": True, "low": 0.01, "high": 0.2},
        }

        with tempfile.TemporaryDirectory() as tmpdir, patch.object(
            config, "EXPECTED_RETURN_ARTIFACT_DIR", tmpdir
        ), patch.object(config, "EXPECTED_RETURN_MIN_REAL_DAYS", 2):
            artifact = build_expected_return_artifact(
                "tomorrow_picks",
                samples,
                baseline_id="baseline_a",
                oos_result=oos_result,
                top_k=3,
            )
            path = save_expected_return_artifact(artifact)
            loaded = load_expected_return_artifact("tomorrow_picks", baseline_id="baseline_a")
            mismatch = load_expected_return_artifact("tomorrow_picks", baseline_id="baseline_b")
            path_exists = os.path.exists(path)

        self.assertTrue(path_exists)
        self.assertTrue(loaded["ok"])
        self.assertEqual(loaded["artifact"]["training_window"]["day_count"], 2)
        self.assertEqual(loaded["artifact"]["baseline_id"], "baseline_a")
        self.assertEqual(loaded["artifact"]["sample_count"], 20)
        self.assertEqual(loaded["artifact"]["oos_result"]["status"], "oos_passed")
        self.assertIn("min_ready_days", loaded["artifact"]["model_params"])
        self.assertTrue(loaded["promotion"]["can_promote"])
        self.assertFalse(mismatch["ok"])
        self.assertEqual(mismatch["status"], "baseline_mismatch")

        missing_ci_artifact = dict(loaded["artifact"])
        missing_ci_artifact["oos_result"] = {key: value for key, value in oos_result.items() if key != "ci"}
        promotion = expected_return_artifact_promotion_gate(missing_ci_artifact, baseline_id="baseline_a")
        self.assertFalse(promotion["can_promote"])
        self.assertEqual(promotion["status"], "ci_blocked")

    def test_expected_return_artifact_expires(self):
        from stock_analyzer.expected_return_model import (
            build_expected_return_artifact,
            load_expected_return_artifact,
            save_expected_return_artifact,
        )

        samples = [
            {
                "signal_date": "2024-01-{:02d}".format((idx % 2) + 1),
                "stored_score": 80,
                "primary_return_net": 1.0,
                "raw": {"score": 80, "risk_penalty": 1},
            }
            for idx in range(20)
        ]
        with tempfile.TemporaryDirectory() as tmpdir, patch.object(
            config, "EXPECTED_RETURN_ARTIFACT_DIR", tmpdir
        ), patch.object(config, "EXPECTED_RETURN_ARTIFACT_MAX_AGE_DAYS", 1):
            artifact = build_expected_return_artifact(
                "tomorrow_picks",
                samples,
                baseline_id="baseline_a",
                oos_result={"ok": True, "status": "shadow_only"},
                created_at=datetime(2024, 1, 1, 9, 0, 0),
            )
            save_expected_return_artifact(artifact)
            loaded = load_expected_return_artifact(
                "tomorrow_picks",
                baseline_id="baseline_a",
                now=datetime(2024, 1, 3, 9, 0, 0),
            )

        self.assertFalse(loaded["ok"])
        self.assertEqual(loaded["status"], "expired")

    def test_expected_return_prediction_can_promote_rank_order_when_enabled(self):
        from stock_analyzer.scoring_core.expected_return import _attach_expected_return_prediction

        rows = [
            {"code": "A", "score": 82, "risk_penalty": 2, "liquidity_score": 82, "momentum_score": 78},
            {"code": "B", "score": 42, "risk_penalty": 2, "liquidity_score": 42, "momentum_score": 38},
        ]
        samples = []
        for idx in range(30):
            low_score_winner = idx < 15
            samples.append(
                {
                    "signal_date": "2024-01-{:02d}".format((idx % 15) + 1),
                    "stored_score": 42 if low_score_winner else 82,
                    "primary_return_net": 2.0 if low_score_winner else -1.0,
                    "max_drawdown": -1.0 if low_score_winner else -3.0,
                    "raw": {
                        "score": 42 if low_score_winner else 82,
                        "risk_penalty": 2,
                        "liquidity_score": 42 if low_score_winner else 82,
                        "momentum_score": 38 if low_score_winner else 78,
                    },
                }
            )

        with patch.object(config, "EXPECTED_RETURN_MIN_REAL_DAYS", 15):
            shadow = _attach_expected_return_prediction("tomorrow_picks", rows, samples=samples, use_ranking=False)
            ranked = _attach_expected_return_prediction("tomorrow_picks", rows, samples=samples, use_ranking=True)

        self.assertEqual([row["code"] for row in shadow], ["A", "B"])
        self.assertEqual([row["code"] for row in ranked], ["B", "A"])
        self.assertEqual(ranked[0]["expected_return_rank"], 1)
        self.assertEqual(ranked[0]["legacy_score_rank"], 2)
        self.assertEqual(ranked[0]["ranking_source"], "expected_return_predicted_net_return")
        self.assertGreater(ranked[0]["predicted_net_return"], ranked[1]["predicted_net_return"])
        self.assertNotIn("rank_score", ranked[0])

    def test_expected_return_prediction_keeps_shadow_order_until_ready(self):
        from stock_analyzer.scoring_core.expected_return import _attach_expected_return_prediction

        rows = [
            {"code": "A", "score": 82, "risk_penalty": 2, "liquidity_score": 82, "momentum_score": 78},
            {"code": "B", "score": 42, "risk_penalty": 2, "liquidity_score": 42, "momentum_score": 38},
        ]
        samples = []
        for idx in range(30):
            low_score_winner = idx < 15
            samples.append(
                {
                    "signal_date": "2024-01-{:02d}".format((idx % 15) + 1),
                    "stored_score": 42 if low_score_winner else 82,
                    "primary_return_net": 2.0 if low_score_winner else -1.0,
                    "max_drawdown": -1.0 if low_score_winner else -3.0,
                    "raw": {
                        "score": 42 if low_score_winner else 82,
                        "risk_penalty": 2,
                        "liquidity_score": 42 if low_score_winner else 82,
                        "momentum_score": 38 if low_score_winner else 78,
                    },
                }
            )

        ranked = _attach_expected_return_prediction("tomorrow_picks", rows, samples=samples, use_ranking=True)

        self.assertEqual([row["code"] for row in ranked], ["A", "B"])
        self.assertEqual(ranked[0]["model_confidence"], "shadow")
        self.assertIn("predicted_net_return", ranked[0])
        self.assertNotIn("rank_score", ranked[0])
        self.assertNotIn("expected_return_rank", ranked[0])

    def test_walk_forward_splits_leave_purge_gap_before_test_dates(self):
        from stock_analyzer.calibrate import _walk_forward_splits

        samples = [
            {"signal_date": "2024-01-{:02d}".format(day), "primary_return_net": 1.0, "raw": {"score": 70}}
            for day in range(1, 7)
        ]

        splits = _walk_forward_splits(samples, requested_folds=2, purge_days=2)

        self.assertEqual(len(splits), 1)
        _train_samples, _test_samples, train_dates, test_dates = splits[0]
        self.assertEqual(train_dates[-1], "2024-01-02")
        self.assertEqual(test_dates[0], "2024-01-05")

    def test_score_calibrator_trains_persists_and_predicts_monotonic_probability(self):
        from stock_analyzer.probability_calibration import load_calibrator, save_calibrator, train_score_calibrator

        samples = []
        for idx in range(30):
            high_score = idx >= 15
            samples.append(
                {
                    "stored_score": 78 if high_score else 42,
                    "primary_return_net": 1.2 if high_score else -0.8,
                    "raw": {"score": 78 if high_score else 42},
                }
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "score_calibrator_tomorrow_picks.json")
            calibrator = train_score_calibrator("tomorrow_picks", samples)
            saved = save_calibrator(calibrator, path=path)
            loaded = load_calibrator("tomorrow_picks", path=saved)

        low = loaded.predict(42)
        high = loaded.predict(78)
        self.assertTrue(loaded.is_fitted)
        self.assertLessEqual(low["calibrated_probability"], high["calibrated_probability"])
        self.assertEqual(high["probability_label"], "高置信")
        self.assertEqual(high["probability_sample_count"], 15)

    def test_attach_score_calibration_adds_probability_fields(self):
        from stock_analyzer.app_support import attach_score_calibration

        class DummyValidationStore:
            def live_weight_samples(self, strategy_name, days=60):
                samples = []
                for idx in range(30):
                    high_score = idx >= 15
                    score = 80 if high_score else 40
                    samples.append(
                        {
                            "stored_score": score,
                            "primary_return_net": 1.0 if high_score else -1.0,
                            "max_drawdown": -1.0 if high_score else -4.0,
                            "raw": {"score": score, "decision_score": score},
                        }
                    )
                return samples

        rows = [{"code": "600001", "score": 82, "decision_score": 82}]
        with tempfile.TemporaryDirectory() as tmpdir, patch.object(config, "SCORE_CALIBRATOR_DIR", tmpdir):
            attach_score_calibration(rows, DummyValidationStore(), "tomorrow_picks", days=60)

        self.assertIn("calibrated_probability", rows[0])
        self.assertGreater(rows[0]["calibrated_probability"], 0.5)
        self.assertEqual(rows[0]["probability_label"], "高置信")
        self.assertEqual(rows[0]["probability_role"], "diagnostic_only")
        self.assertFalse(rows[0]["probability_trading_enabled"])
        self.assertIn("仅供校准观察", rows[0]["score_note"])
        self.assertIn("decision_calibration", rows[0])

    def test_meta_label_model_predicts_confidence_from_validation_samples(self):
        from stock_analyzer.meta_labeling import predict_meta_confidence, train_meta_label_model

        samples = []
        for idx in range(60):
            strong = idx >= 30
            score = 82 if strong else 42
            risk_penalty = 1 if strong else 11
            samples.append(
                {
                    "stored_score": score,
                    "primary_return_net": 1.4 if strong else -0.9,
                    "raw": {"score": score, "risk_penalty": risk_penalty},
                }
            )

        with patch.object(config, "META_LABELING_MIN_SAMPLES", 20):
            model = train_meta_label_model("tomorrow_picks", samples)
            strong = predict_meta_confidence({"score": 82, "risk_penalty": 1}, model)
            weak = predict_meta_confidence({"score": 42, "risk_penalty": 11}, model)

        self.assertTrue(model["is_fitted"])
        self.assertGreater(strong["confidence"], weak["confidence"])
        self.assertEqual(strong["action"], "full")
        self.assertEqual(weak["action"], "skip")

    def test_attach_meta_labeling_adds_shadow_fields_without_changing_position(self):
        from stock_analyzer.app_support import attach_meta_labeling

        class DummyValidationStore:
            def live_weight_samples(self, strategy_name, days=120):
                samples = []
                for idx in range(60):
                    strong = idx >= 30
                    score = 82 if strong else 42
                    samples.append(
                        {
                            "stored_score": score,
                            "primary_return_net": 1.2 if strong else -1.0,
                            "raw": {"score": score, "risk_penalty": 1 if strong else 12},
                        }
                    )
                return samples

        rows = [
            {
                "code": "600001",
                "score": 82,
                "risk_penalty": 1,
                "trade_action": {"action": "buy_confirmed", "position_size": 1.0},
            }
        ]
        with patch.object(config, "META_LABELING_MIN_SAMPLES", 20), patch.object(
            config, "ENABLE_META_LABELING", True
        ), patch.object(config, "META_LABELING_ENFORCE_ACTION", True):
            attach_meta_labeling(rows, DummyValidationStore(), "tomorrow_picks", days=120)

        self.assertIn("meta_labeling", rows[0])
        self.assertIn("meta_confidence", rows[0])
        self.assertFalse(rows[0]["meta_labeling"]["enabled"])
        self.assertEqual(rows[0]["trade_action"], {"action": "buy_confirmed", "position_size": 1.0})
        self.assertEqual(rows[0]["trade_action"]["position_size"], 1.0)

    def test_event_alpha_scores_independent_catalysts(self):
        from stock_analyzer.event_alpha import event_alpha_score

        strong = event_alpha_score(
            [
                {
                    "type": "业绩预增",
                    "confidence": 0.9,
                    "time_sensitivity": "明天",
                    "already_priced_in": False,
                }
            ],
            strategy_name="tomorrow_picks",
        )
        stale = event_alpha_score(
            [
                {
                    "type": "业绩预增",
                    "confidence": 0.9,
                    "time_sensitivity": "长期",
                    "already_priced_in": True,
                }
            ],
            strategy_name="tomorrow_picks",
        )

        self.assertTrue(strong["event_alpha_active"])
        self.assertGreater(strong["event_alpha_score"], stale["event_alpha_score"])
        self.assertTrue(strong["hits"])

    def test_attach_event_alpha_uses_deepseek_event_fields(self):
        from stock_analyzer.event_alpha import attach_event_alpha

        rows = [
            {
                "code": "600001",
                "deepseek_event_type": "订单",
                "deepseek_catalyst_score": 82,
                "deepseek_catalyst_strength": 80,
                "deepseek_time_sensitivity": "2-5天",
                "deepseek_already_priced_in": False,
            }
        ]

        attach_event_alpha(rows, strategy_name="swing_picks")

        self.assertIn("event_alpha", rows[0])
        self.assertGreater(rows[0]["event_alpha_score"], 50.0)
        self.assertTrue(rows[0]["event_alpha"]["hits"])
        self.assertEqual(rows[0]["event_alpha"]["mode"], "research_only")
        self.assertFalse(rows[0]["event_alpha"]["trading_enabled"])
        self.assertAlmostEqual(rows[0]["event_alpha"]["hits"][0]["confidence"], 0.82, places=4)

    def test_attach_event_alpha_uses_catalyst_strength_when_score_missing(self):
        from stock_analyzer.event_alpha import attach_event_alpha

        rows = [
            {
                "code": "600001",
                "deepseek_event_type": "订单",
                "deepseek_catalyst_strength": 80,
                "deepseek_time_sensitivity": "2-5天",
                "deepseek_already_priced_in": False,
            }
        ]

        attach_event_alpha(rows, strategy_name="swing_picks")

        self.assertTrue(rows[0]["event_alpha"]["hits"])
        self.assertAlmostEqual(rows[0]["event_alpha"]["hits"][0]["confidence"], 0.8, places=4)

    def test_ensemble_score_blends_independent_model_scores(self):
        from stock_analyzer.ensemble import ensemble_score

        result = ensemble_score(
            {"price_volume": 70, "expected_return": 80, "event": 90},
            {"price_volume": 0.5, "expected_return": 0.3, "event": 0.2},
        )

        self.assertAlmostEqual(result["ensemble_score"], 77.0)
        self.assertLess(result["agreement"], 1.0)
        self.assertEqual(set(result["model_scores"]), {"price_volume", "expected_return", "event"})

    def test_attach_ensemble_score_adds_shadow_fields(self):
        from stock_analyzer.ensemble import attach_ensemble_score

        rows = [
            {
                "code": "600001",
                "score": 70,
                "rank_score": 78,
                "event_alpha_score": 86,
                "calibrated_probability": 0.64,
                "meta_confidence": 0.62,
            }
        ]

        with patch.object(config, "ENABLE_ENSEMBLE", True):
            attach_ensemble_score(rows)

        self.assertIn("ensemble", rows[0])
        self.assertIn("ensemble_score", rows[0])
        self.assertFalse(rows[0]["ensemble"]["enabled"])
        self.assertEqual(rows[0]["ensemble"]["mode"], "shadow_only")
        self.assertEqual(rows[0]["score"], 70)

    def test_live_weight_calibration_keeps_weights_when_samples_insufficient(self):
        from stock_analyzer.calibrate import calibrate_live_weights

        class FakeValidationStore:
            def __init__(self, db_path):
                self.db_path = db_path

            def live_weight_samples(self, strategy_name, days=120):
                return []

        with tempfile.TemporaryDirectory() as tmpdir:
            weights_path = "{}/weights.json".format(tmpdir)
            with patch.object(config, "WEIGHTS_OVERRIDE_PATH", weights_path), patch.object(
                config, "CALIBRATE_MIN_SAMPLES", 30
            ), patch("stock_analyzer.calibrate.StrategyValidationStore", FakeValidationStore):
                result = calibrate_live_weights("tomorrow_picks", db_path="{}/validation.sqlite3".format(tmpdir), dry_run=False)

        self.assertEqual(result["status"], "insufficient_samples")
        self.assertFalse(os.path.exists(weights_path))

    def test_live_sample_evaluation_uses_alpha_not_absolute_beta(self):
        from stock_analyzer.calibrate import _evaluate_live_samples

        samples = []
        for score in (90, 70, 50):
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
        self.assertEqual(metrics["return_series"], [5.0])
        self.assertIn("sortino", metrics)

    def test_live_weight_calibration_requires_oos_improvement_to_write(self):
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

        class FakeValidationStore:
            def __init__(self, db_path):
                self.db_path = db_path

            def live_weight_samples(self, strategy_name, days=120):
                return samples

        with tempfile.TemporaryDirectory() as tmpdir:
            weights_path = "{}/weights.json".format(tmpdir)
            with patch.object(config, "WEIGHTS_OVERRIDE_PATH", weights_path), patch.object(
                config, "CALIBRATE_MIN_SAMPLES", 2
            ), patch("stock_analyzer.calibrate.StrategyValidationStore", FakeValidationStore), patch(
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

        class FakeValidationStore:
            def __init__(self, db_path):
                self.db_path = db_path

            def live_weight_samples(self, strategy_name, days=120):
                return samples

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(config, "CALIBRATE_MIN_SAMPLES", 2), patch.object(
                config, "CALIBRATE_MIN_COVERAGE", 0.5
            ), patch("stock_analyzer.calibrate.StrategyValidationStore", FakeValidationStore):
                result = calibrate_live_weights("tomorrow_picks", db_path="{}/v.sqlite3".format(tmpdir), dry_run=False)

        self.assertEqual(result["status"], "insufficient_factor_coverage")
        self.assertEqual(result["avg_data_coverage"], 0.0)

    def test_expected_return_ranker_reports_oos_shadow_status(self):
        from stock_analyzer.calibrate import evaluate_expected_return_ranker

        samples = []
        for day in range(1, 9):
            samples.append(
                {
                    "signal_date": "2024-03-{:02d}".format(day),
                    "stored_score": 70,
                    "primary_return_net": 1.5,
                    "max_drawdown": -1.0,
                    "raw": {"score": 70, "risk_penalty": 1},
                }
            )
            samples.append(
                {
                    "signal_date": "2024-03-{:02d}".format(day),
                    "stored_score": 90,
                    "primary_return_net": -1.0,
                    "max_drawdown": -4.0,
                    "raw": {"score": 90, "risk_penalty": 8},
                }
            )

        with patch.object(config, "CALIBRATE_WALK_FORWARD_FOLDS", 3):
            result = evaluate_expected_return_ranker("tomorrow_picks", samples, top_k=1)

        self.assertTrue(result["ok"])
        self.assertIn(result["status"], {"shadow_only", "oos_passed", "fdr_blocked", "ci_blocked"})
        self.assertIn("can_promote", result)
        self.assertIn("ci", result)
        self.assertIn("predicted_net_return_oos_objective", result)
        self.assertEqual(result["fold_count"], 3)
        self.assertTrue(result["folds"])
        self.assertIn("predicted_net_return_oos_objective", result["folds"][0])

    def test_meta_labeling_gate_reports_oos_shadow_improvement(self):
        from stock_analyzer.calibrate import evaluate_meta_labeling_gate

        samples = []
        for day in range(1, 11):
            date = "2024-06-{:02d}".format(day)
            samples.append(
                {
                    "signal_date": date,
                    "stored_score": 82,
                    "primary_return_net": -1.2,
                    "max_drawdown": -4.0,
                    "raw": {"score": 82, "risk_penalty": 12},
                }
            )
            samples.append(
                {
                    "signal_date": date,
                    "stored_score": 42,
                    "primary_return_net": 1.4,
                    "max_drawdown": -1.0,
                    "raw": {"score": 42, "risk_penalty": 1},
                }
            )

        with patch.object(config, "CALIBRATE_WALK_FORWARD_FOLDS", 3), patch.object(
            config, "META_LABELING_MIN_SAMPLES", 4
        ), patch.object(config, "CALIBRATE_IMPROVE_MARGIN", 0.01), patch.object(
            config, "ENABLE_CALIBRATE_FDR", False
        ):
            result = evaluate_meta_labeling_gate("tomorrow_picks", samples, top_k=1)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "oos_passed")
        self.assertGreater(result["meta_oos_objective"], result["baseline_oos_objective"])
        self.assertGreater(result["positive_folds"], result["fold_count"] // 2)
        self.assertTrue(result["can_enforce"])

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
        self.assertGreater(
            result["weights_by_regime"]["risk_off"]["liquidity"],
            result["weights_by_regime"]["risk_off"]["momentum"],
        )

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

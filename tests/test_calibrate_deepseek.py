import json
import os
import tempfile
import unittest
from unittest.mock import patch

from stock_analyzer import config
from stock_analyzer.calibrate import calibrate_blend_alpha, evaluate_deepseek_rule


class CalibrateDeepSeekTest(unittest.TestCase):
    def test_evaluate_deepseek_rule_requires_oos_improvement(self):
        samples = _samples_for_rule()
        rule = {"field": "pct_chg", "operator": ">", "threshold": 5, "penalty": 50, "reason": "过热"}
        with patch.object(config, "CALIBRATE_WALK_FORWARD_FOLDS", 2), patch.object(
            config, "CALIBRATE_IMPROVE_MARGIN", 0.05
        ):
            result = evaluate_deepseek_rule("short_term", rule, samples, top_k=1, dry_run=True)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "oos_passed")
        self.assertTrue(result["can_apply"])
        self.assertGreater(result["oos_improvement"], 0)
        self.assertGreater(result["positive_folds"], result["fold_count"] // 2)

    def test_calibrate_blend_alpha_returns_zero_when_no_oos_edge_else_best_alpha(self):
        samples = _samples_for_rule()
        with patch.object(config, "CALIBRATE_WALK_FORWARD_FOLDS", 2), patch.object(
            config, "CALIBRATE_IMPROVE_MARGIN", 0.05
        ):
            result = calibrate_blend_alpha("short_term", samples, top_k=1, alphas=(0.0, 0.3), dry_run=True)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "oos_passed")
        self.assertTrue(result["can_apply"])
        self.assertEqual(result["selected_alpha"], 0.3)

    def test_calibrate_blend_alpha_can_persist_alpha_zero_from_dry_run(self):
        samples = _samples_for_bad_alpha()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "weights.json")
            with patch.object(config, "WEIGHTS_OVERRIDE_PATH", path), patch.object(
                config, "CALIBRATE_WALK_FORWARD_FOLDS", 2
            ), patch.object(config, "CALIBRATE_IMPROVE_MARGIN", 0.05), patch.object(
                config, "DEEPSEEK_WRITE_ALPHA_ZERO", True
            ):
                result = calibrate_blend_alpha(
                    "short_term",
                    samples,
                    top_k=1,
                    alphas=(0.0, 0.3),
                    dry_run=True,
                    write_alpha_zero=True,
                )
                with open(path, "r", encoding="utf-8") as handle:
                    payload = json.load(handle)

        self.assertEqual(result["status"], "alpha_zero_written")
        self.assertFalse(result["can_apply"])
        self.assertEqual(result["selected_alpha"], 0.0)
        self.assertEqual(payload["deepseek_blend_alpha"]["short_term"], 0.0)


def _samples_for_rule():
    samples = []
    for index in range(1, 6):
        date = f"2026-01-0{index}"
        samples.append(_sample(date, "BAD", rank=1, local_rank=1, score=95, deepseek_score=20, pct_chg=8, ret=-2))
        samples.append(_sample(date, "GOOD", rank=2, local_rank=2, score=80, deepseek_score=98, pct_chg=2, ret=2))
    return samples


def _samples_for_bad_alpha():
    samples = []
    for index in range(1, 6):
        date = f"2026-02-0{index}"
        samples.append(_sample(date, "GOOD", rank=1, local_rank=1, score=95, deepseek_score=20, pct_chg=2, ret=2))
        samples.append(_sample(date, "BAD", rank=2, local_rank=2, score=80, deepseek_score=98, pct_chg=2, ret=-2))
    return samples


def _sample(date, suffix, rank, local_rank, score, deepseek_score, pct_chg, ret):
    raw = {
        "code": f"{date}-{suffix}",
        "score": score,
        "pct_chg": pct_chg,
        "local_rank": local_rank,
        "deepseek_horizon_score": deepseek_score,
        "deepseek_penalty": 0,
        "momentum_score": score,
        "liquidity_score": score,
        "industry_score": score,
        "sentiment_score": score,
        "risk_guard_score": score,
    }
    return {
        "signal_date": date,
        "code": raw["code"],
        "rank": rank,
        "stored_score": score,
        "raw": raw,
        "primary_return_net": ret,
        "next_open_return": 0,
        "max_drawdown": -1,
    }


if __name__ == "__main__":
    unittest.main()

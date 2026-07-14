import unittest

import pandas as pd

from stock_analyzer.performance import (
    json_loads_cached,
    records_from_columns,
    validation_metrics_cache_key,
)
from stock_analyzer.validation_cache import ValidationMetricsCache


class PerformanceContractsTest(unittest.TestCase):
    def test_validation_metrics_cache_key_includes_strategy_baseline_and_days(self):
        key = validation_metrics_cache_key(" tomorrow_picks ", " baseline-v2 ", "20")

        self.assertEqual(key, ("tomorrow_picks", "baseline-v2", 20))

    def test_validation_cache_uses_repository_baseline_key(self):
        class Repository:
            baseline_id = "baseline-v1"

            def metrics_cache_key(self, strategy_name, days):
                return validation_metrics_cache_key(strategy_name, self.baseline_id, days)

        class Store:
            def __init__(self):
                self.repository = Repository()
                self.calls = 0

            def metrics(self, strategy_name, days=20):
                self.calls += 1
                return {"calls": self.calls, "strategy": strategy_name, "days": days}

        store = Store()
        cache = ValidationMetricsCache(store, ttl_seconds=60)

        first = cache.metrics("tomorrow_picks", 20)
        second = cache.metrics("tomorrow_picks", 20)
        store.repository.baseline_id = "baseline-v2"
        third = cache.metrics("tomorrow_picks", 20)

        self.assertEqual(first, second)
        self.assertEqual(store.calls, 2)
        self.assertEqual(third["calls"], 2)

    def test_records_from_columns_sorts_limits_and_preserves_source_frame(self):
        frame = pd.DataFrame(
            {
                "code": ["000001", "000002", "000003"],
                "name": ["A", "B", "C"],
                "pct_chg": [1.0, 5.0, 3.0],
                "extra": [10, 20, 30],
            }
        )
        original = frame.copy(deep=True)

        records = records_from_columns(frame, ["code", "name"], limit=2, sort_by="pct_chg", ascending=False)

        self.assertEqual(records, [{"code": "000002", "name": "B"}, {"code": "000003", "name": "C"}])
        pd.testing.assert_frame_equal(frame, original)

    def test_json_loads_cached_reuses_parsed_payload(self):
        cache = {}
        first = json_loads_cached('{"code": "000001"}', cache=cache)
        second = json_loads_cached('{"code": "000001"}', cache=cache)

        self.assertIs(first, second)
        self.assertEqual(first["code"], "000001")


if __name__ == "__main__":
    unittest.main()



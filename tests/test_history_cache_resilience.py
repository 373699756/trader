import os
import tempfile
import unittest

import pandas as pd

from stock_analyzer.history_cache import HistoryCache
from stock_analyzer.providers import MarketDataProvider, ProviderStatus, TimedCache
from stock_analyzer.services.factor_sentiment_refresh import FactorSentimentRefreshService


def sample_history():
    return pd.DataFrame(
        {
            "trade_date": ["20260701", "20260702"],
            "code": ["600001", "600001"],
            "open": [10.0, 10.2],
            "high": [10.4, 10.5],
            "low": [9.9, 10.1],
            "price": [10.2, 10.4],
            "turnover": [2e8, 2.2e8],
            "volume": [1000, 1100],
        }
    )


class HistoryCacheResilienceTest(unittest.TestCase):
    def test_relative_cache_path_remains_valid_after_working_directory_changes(self):
        original_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmpdir, tempfile.TemporaryDirectory() as other_dir:
            try:
                os.chdir(tmpdir)
                cache = HistoryCache("cache/history.sqlite3")
                cache.set("600001", sample_history())
                os.chdir(other_dir)

                loaded = cache.get("600001", 10)
            finally:
                os.chdir(original_cwd)

        self.assertTrue(os.path.isabs(cache.db_path))
        self.assertEqual(len(loaded), 2)

    def test_provider_uses_local_history_when_sqlite_cache_is_unavailable(self):
        class BrokenCache:
            def get(self, code, days):
                raise OSError("cache unavailable")

            def is_fresh(self, code):
                raise AssertionError("freshness should not run after a failed read")

        provider = object.__new__(MarketDataProvider)
        provider.status = ProviderStatus()
        provider._history_cache = BrokenCache()
        provider._load_local_history = lambda code, days: sample_history()

        loaded = provider.get_cached_history("600001", days=2)

        self.assertEqual(len(loaded), 2)
        self.assertTrue(any("历史缓存读取失败" in error for error in provider.status.errors))

    def test_background_refresh_swallows_provider_error_and_releases_codes(self):
        class BrokenProvider:
            def __init__(self):
                self.errors = []

            def prefetch_history(self, codes, days=90):
                raise OSError("database unavailable")

            def _record_sentiment_error(self, message):
                self.errors.append(message)

        provider = BrokenProvider()
        service = FactorSentimentRefreshService(
            refresh_history=lambda codes: provider.prefetch_history(list(codes), days=90),
            score_sentiment=lambda _code, _name: {},
            sentiment_cache=TimedCache(60),
            normalize_code=lambda value: str(value or "")[-6:],
            record_error=provider._record_sentiment_error,
        )

        self.assertTrue(service.schedule_history(["600001"]))
        service.stop(timeout_seconds=1.0)

        self.assertTrue(any("后台历史因子刷新失败" in error for error in provider.errors))
        self.assertEqual(service.status()["history"]["refreshing_items"], 0)
        self.assertEqual(service.status()["history"]["failure_count"], 1)


if __name__ == "__main__":
    unittest.main()

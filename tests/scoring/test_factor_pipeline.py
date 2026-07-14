import threading

import pandas as pd
from unittest.mock import patch

from stock_analyzer import app_support, config
from stock_analyzer.app_support import attach_alphalite_factors as _attach_alphalite_factors
from stock_analyzer.factor_ic import compute_factor_ic
from stock_analyzer.factors import compute_alphalite_for_stock
from stock_analyzer.fundamentals import attach_fundamental_factors, load_fundamentals
from stock_analyzer.history_cache import HistoryCache
from stock_analyzer.providers import MarketDataProvider, ProviderStatus, TimedCache
from stock_analyzer.scoring import prepare_candidates


def _quote_frame():
    return pd.DataFrame(
        [
            {
                "code": "600001",
                "name": "样本",
                "price": 20,
                "pct_chg": 3.0,
                "turnover": 900000000,
                "turnover_rate": 7,
                "volume_ratio": 2.0,
                "sixty_day_pct": 18,
                "amplitude": 5,
            }
        ]
    )


def _history_frame(rows=70):
    return pd.DataFrame(
        {
            "trade_date": [f"202607{idx:02d}" for idx in range(1, rows + 1)],
            "code": ["600001"] * rows,
            "open": range(1, rows + 1),
            "high": [value * 1.01 for value in range(1, rows + 1)],
            "low": [value * 0.99 for value in range(1, rows + 1)],
            "price": [float(value) for value in range(1, rows + 1)],
            "turnover": [100000000] * rows,
            "volume": [1000000] * (rows - 1) + [3000000],
        }
    )


def test_alphalite_attach_uses_cache_only_by_default_without_sync_fetch():
    class SlowProvider:
        def get_cached_history(self, code, days=90):
            return pd.DataFrame()

        def get_history(self, code, days=90):
            raise AssertionError("request path should not fetch remote history by default")

    with patch.object(config, "ENABLE_HISTORY_FACTORS", True), patch.object(
        config,
        "HISTORY_FACTORS_FETCH_ON_REQUEST",
        False,
    ):
        enriched = _attach_alphalite_factors(SlowProvider(), TimedCache(1), prepare_candidates(_quote_frame()))

    assert "alphalite_factor_ready" in enriched.columns
    assert enriched.iloc[0]["alphalite_factor_ready"] == 0.0


def test_alphalite_attach_schedules_missing_history_without_blocking_request():
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
        config,
        "HISTORY_FACTORS_FETCH_ON_REQUEST",
        True,
    ):
        enriched = _attach_alphalite_factors(
            BackgroundProvider(),
            TimedCache(1),
            prepare_candidates(_quote_frame()),
        )

    assert enriched.iloc[0]["alphalite_factor_ready"] == 0.0
    assert refreshed.wait(timeout=1.0)


def test_alphalite_factor_cache_reuses_same_local_history():
    cache = TimedCache(60)

    class LocalProvider:
        def get_cached_history(self, code, days=90):
            return pd.DataFrame()

    with patch.object(config, "ENABLE_HISTORY_FACTORS", True), patch.object(
        config,
        "HISTORY_FACTORS_FETCH_ON_REQUEST",
        False,
    ), patch(
        "stock_analyzer.app_support.load_local_history_frames",
        return_value={"600001": _history_frame(30)},
    ), patch(
        "stock_analyzer.app_support.build_alphalite_factors",
        wraps=app_support.build_alphalite_factors,
    ) as build_mock:
        first = _attach_alphalite_factors(LocalProvider(), cache, prepare_candidates(_quote_frame()))
        second = _attach_alphalite_factors(LocalProvider(), cache, prepare_candidates(_quote_frame()))

    assert build_mock.call_count == 1
    assert first.iloc[0]["alphalite_factor_ready"] == second.iloc[0]["alphalite_factor_ready"]


def test_history_cache_falls_back_to_local_history_when_sqlite_cache_fails():
    class BrokenCache:
        def get(self, code, days):
            raise OSError("cache unavailable")

        def is_fresh(self, code):
            raise AssertionError("freshness should not run after a failed read")

    provider = object.__new__(MarketDataProvider)
    provider.status = ProviderStatus()
    provider._history_cache = BrokenCache()
    provider._load_local_history = lambda code, days: _history_frame(2)

    loaded = provider.get_cached_history("600001", days=2)

    assert len(loaded) == 2
    assert any("历史缓存读取失败" in error for error in provider.status.errors)


def test_history_cache_round_trip(tmp_path):
    cache = HistoryCache(str(tmp_path / "history.sqlite3"))
    cache.set("600001", _history_frame(5))

    loaded = cache.get("600001", 3)

    assert len(loaded) == 3
    assert loaded.iloc[-1]["trade_date"] == "20260705"


def test_alphalite_and_enhanced_factor_contracts():
    trend = _history_frame(70)
    factors = compute_alphalite_for_stock("600001", trend)

    assert factors["ma_bull_aligned"] == 1.0
    assert factors["vol_ma5_ratio"] > 1.5
    assert "ma60_gap" in factors
    assert "ma10_gap" in factors

    with patch.object(config, "ENABLE_ENHANCED_FACTORS", False):
        disabled = compute_alphalite_for_stock("600001", trend.tail(25))
    with patch.object(config, "ENABLE_ENHANCED_FACTORS", True):
        enabled = compute_alphalite_for_stock("600001", trend.tail(25))

    assert disabled["close_vs_vwap"] == 0.0
    assert enabled["price_position_20d"] > 50.0


def test_fundamental_factors_and_factor_ic():
    df = pd.DataFrame(
        [
            {"code": "600001", "roe": 18, "gross_margin": 45, "debt_ratio": 25, "pe_dynamic": 12, "pb": 1.2},
            {"code": "600002", "roe": 5, "gross_margin": 18, "debt_ratio": 70, "pe_dynamic": 60, "pb": 8.0},
            {"code": "600003", "roe": 12, "gross_margin": 30, "debt_ratio": 45, "pe_dynamic": 25, "pb": 2.5},
        ]
    )

    with patch.object(config, "ENABLE_FUNDAMENTALS", True):
        enriched = attach_fundamental_factors(df)
    samples = [
        {"raw": {"fundamental_quality_score": row["fundamental_quality_score"]}, "primary_return_net": ret}
        for row, ret in zip(enriched.to_dict("records"), [3.0, -2.0, 1.0])
    ]
    ic = compute_factor_ic(samples, factor_keys=["fundamental_quality_score"])

    assert enriched.iloc[0]["fundamental_quality_score"] > enriched.iloc[1]["fundamental_quality_score"]
    assert ic["ic"]["fundamental_quality_score"]["ic"] > 0


def test_fundamental_loader_uses_daily_cache(tmp_path):
    class FakeProvider:
        def __init__(self):
            self.calls = 0

        def get_fundamental_factors(self, codes=None):
            self.calls += 1
            return {"600001": {"roe": 18, "gross_margin": 40, "debt_ratio": 25, "pe_dynamic": 12, "pb": 1.5}}

    provider = FakeProvider()
    with patch.object(config, "ENABLE_FUNDAMENTALS", True), patch.object(
        config,
        "FUNDAMENTAL_CACHE_PATH",
        str(tmp_path / "fundamentals.json"),
    ), patch.object(config, "FUNDAMENTAL_CACHE_HOURS", 24):
        first = load_fundamentals(provider, codes=["600001"])
        second = load_fundamentals(provider, codes=["600001"])

    assert first["status"] == "ok"
    assert second["status"] == "ok"
    assert provider.calls == 1
    assert "600001" in second["items"]

import threading
import time
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from stock_analyzer import config
from stock_analyzer.app import create_app
from stock_analyzer.candidate_pipeline import CandidatePipeline
from stock_analyzer.providers import MarketDataProvider, TimedCache


def _quotes():
    return pd.DataFrame(
        [{"code": "600001", "name": "样本", "price": 10.0, "pct_chg": 1.0, "turnover": 100000000}]
    )


def test_web_quotes_return_snapshot_without_waiting_for_remote_download():
    provider = MarketDataProvider(web_nonblocking=True)
    snapshot = _quotes()
    snapshot.attrs["snapshot_mtime"] = "2026-07-12T09:00:00"
    provider._load_quote_snapshot = MagicMock(return_value=snapshot)
    provider.refresh_realtime_quotes_async = MagicMock(return_value=True)
    provider._fetch_eastmoney_quotes = MagicMock(side_effect=AssertionError("web path must not fetch synchronously"))

    result = provider.get_realtime_quotes()

    assert result is snapshot
    assert provider.status.quotes_source == "本地快照"
    provider.refresh_realtime_quotes_async.assert_called_once_with()
    provider._fetch_eastmoney_quotes.assert_not_called()


def test_web_quotes_without_snapshot_start_background_refresh_and_fail_fast():
    provider = MarketDataProvider(web_nonblocking=True)
    provider._load_quote_snapshot = MagicMock(return_value=None)
    provider.refresh_realtime_quotes_async = MagicMock(return_value=True)
    provider._fetch_eastmoney_quotes = MagicMock(side_effect=AssertionError("web path must not fetch synchronously"))
    provider._fetch_akshare_quotes = MagicMock(side_effect=AssertionError("slow fallback must stay off-request"))

    with pytest.raises(RuntimeError, match="后台刷新"):
        provider.get_realtime_quotes()

    provider.refresh_realtime_quotes_async.assert_called_once_with()
    provider._fetch_eastmoney_quotes.assert_not_called()
    provider._fetch_akshare_quotes.assert_not_called()


def test_background_quote_refresh_is_single_flight():
    provider = MarketDataProvider()
    with patch("stock_analyzer.providers.threading.Thread") as thread_class:
        thread_class.return_value.start.return_value = None

        assert provider.refresh_realtime_quotes_async()
        assert not provider.refresh_realtime_quotes_async()

    thread_class.assert_called_once()
    assert thread_class.call_args.kwargs["daemon"] is True


def test_background_quote_refresh_recovers_when_thread_cannot_start():
    provider = MarketDataProvider()
    with patch("stock_analyzer.providers.threading.Thread") as thread_class:
        thread_class.return_value.start.side_effect = RuntimeError("thread unavailable")

        assert not provider.refresh_realtime_quotes_async()

    status = provider.quote_refresh_status()
    assert not status["running"]
    assert status["last_error"] == "thread unavailable"


def test_background_quote_refresh_skips_akshare_serial_quote_download():
    provider = MarketDataProvider()
    provider._fetch_eastmoney_quotes = MagicMock(side_effect=RuntimeError("eastmoney unavailable"))
    provider._fetch_akshare_quotes = MagicMock(side_effect=AssertionError("serial AkShare download must not run"))
    provider._fetch_sina_quotes = MagicMock(return_value=_quotes())
    provider._save_quote_snapshot = MagicMock()

    with patch.object(config, "ALLOW_SLOW_QUOTE_FALLBACK", True):
        provider._refresh_realtime_quotes_worker()

    provider._fetch_akshare_quotes.assert_not_called()
    provider._fetch_sina_quotes.assert_called_once_with()
    assert provider.status.quotes_source == "新浪并发行情"
    assert provider.quote_refresh_status()["last_error"] == ""


def test_web_request_does_not_wait_for_running_remote_download():
    provider = MarketDataProvider(web_nonblocking=True)
    download_started = threading.Event()
    release_download = threading.Event()

    def slow_remote_download():
        download_started.set()
        release_download.wait(timeout=2)
        return _quotes()

    provider._load_quote_snapshot = MagicMock(return_value=None)
    provider._fetch_eastmoney_quotes = slow_remote_download
    started_at = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match="后台刷新"):
            provider.get_realtime_quotes()
        elapsed = time.monotonic() - started_at

        assert elapsed < 0.2
        assert download_started.wait(timeout=1)
        assert provider.quote_refresh_status()["running"]
    finally:
        release_download.set()


def test_candidate_pipeline_keeps_existing_provider_contract():
    class Provider:
        def get_realtime_quotes(self):
            return _quotes()

    class Caches:
        quotes_cache = TimedCache(30)

    result = CandidatePipeline(Provider(), Caches()).current_quotes()

    assert result.iloc[0]["code"] == "600001"


def test_cold_web_endpoints_return_while_quote_download_runs(tmp_path):
    download_started = threading.Event()
    release_download = threading.Event()

    def slow_remote_download(_provider):
        download_started.set()
        release_download.wait(timeout=2)
        return _quotes()

    patches = (
        patch.object(config, "VALIDATION_DB_PATH", str(tmp_path / "validation.sqlite3")),
        patch.object(config, "RECOMMENDATION_SNAPSHOT_PATH", str(tmp_path / "recommendations.json")),
        patch.object(config, "QUOTE_SNAPSHOT_PATH", str(tmp_path / "quotes.json")),
        patch.object(config, "VALIDATION_AUTO_UPDATE_ENABLED", False),
        patch.object(config, "VALIDATION_AUTO_SNAPSHOT_ENABLED", False),
        patch.object(config, "ENABLE_EVENT_RISK", False),
        patch.object(config, "ENABLE_FUNDAMENTALS", False),
        patch.object(config, "ENABLE_INLINE_SENTIMENT", False),
        patch.object(config, "ENABLE_MARKET_NEWS", False),
        patch.object(config, "ENABLE_HISTORY_FACTORS", False),
        patch.object(config, "ENABLE_HOT_RANKS", False),
        patch.object(config, "ENABLE_INDUSTRY_STRENGTH", False),
        patch.object(MarketDataProvider, "_fetch_eastmoney_quotes", slow_remote_download),
    )
    try:
        for item in patches:
            item.start()
        app = create_app()
        client = app.test_client()

        started_at = time.monotonic()
        index_response = client.get("/")
        recommendation_response = client.get("/api/recommendations?top_n=18&market=all")
        elapsed = time.monotonic() - started_at

        assert elapsed < 0.5
        assert index_response.status_code == 200
        assert recommendation_response.status_code == 200
        payload = recommendation_response.get_json()
        assert payload["ok"]
        assert payload["meta"]["fallback"] == "async_refresh_pending"
        assert download_started.wait(timeout=1)
    finally:
        release_download.set()
        deadline = time.monotonic() + 1
        while app.extensions["app_container"].provider.quote_refresh_status()["running"] and time.monotonic() < deadline:
            time.sleep(0.01)
        for item in reversed(patches):
            item.stop()

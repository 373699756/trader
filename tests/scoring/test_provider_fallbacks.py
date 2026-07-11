from datetime import datetime
from unittest.mock import MagicMock, patch

import pandas as pd

from helpers import app_patch_context
from stock_analyzer import config
from stock_analyzer.daily_data import DailyMarketDataStore
from stock_analyzer.normalization import rename_known_columns
from stock_analyzer.providers import MarketDataProvider, _normalize_eastmoney_spot, _request_eastmoney_page


def test_eastmoney_normalization_maps_required_quote_fields():
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
                "f124": str(int(datetime(2026, 7, 9, 15, 0).timestamp())),
            }
        ]
    )

    result = rename_known_columns(_normalize_eastmoney_spot(raw))

    assert result.iloc[0]["code"] == "600001"
    assert result.iloc[0]["name"] == "样本股份"
    assert result.iloc[0]["price"] == 12.3
    assert result.iloc[0]["turnover"] == 90000000
    assert str(result.attrs.get("quote_timestamp") or "").startswith("2026-07-09T15:00")


def test_provider_prefers_direct_eastmoney_quotes():
    provider = MarketDataProvider()
    provider._fetch_akshare_quotes = lambda: (_ for _ in ()).throw(RuntimeError("akshare failed"))
    provider._fetch_eastmoney_quotes = lambda: pd.DataFrame(
        [{"code": "600001", "name": "样本股份", "price": 12, "pct_chg": 3, "turnover": 90000000}]
    )

    quotes = provider.get_realtime_quotes()

    assert str(quotes.iloc[0]["code"]).zfill(6) == "600001"
    assert provider.status.quotes_source == "东方财富直连"
    assert provider.status.errors == []


def test_provider_fails_fast_when_quote_fallback_disabled(tmp_path):
    provider = MarketDataProvider()
    provider._fetch_eastmoney_quotes = lambda: (_ for _ in ()).throw(RuntimeError("eastmoney failed"))

    with patch.object(config, "QUOTE_SNAPSHOT_PATH", str(tmp_path / "missing.json")), patch.object(
        config,
        "ALLOW_SLOW_QUOTE_FALLBACK",
        False,
    ):
        try:
            provider.get_realtime_quotes()
        except RuntimeError:
            raised = True
        else:
            raised = False

    assert raised
    assert provider.status.quotes_source == "unavailable"
    assert "东方财富直连行情失败" in provider.status.errors[0]


def test_provider_uses_quote_snapshot_when_live_quote_sources_fail(tmp_path):
    provider = MarketDataProvider()
    snapshot = pd.DataFrame(
        [{"code": "600001", "name": "样本股份", "price": 12, "pct_chg": 3, "turnover": 90000000}]
    )
    snapshot.attrs["quote_timestamp"] = "2026-07-09T15:00:00"
    provider._fetch_eastmoney_quotes = lambda: (_ for _ in ()).throw(RuntimeError("eastmoney failed"))
    provider._fetch_akshare_quotes = lambda: (_ for _ in ()).throw(RuntimeError("akshare failed"))
    provider._fetch_sina_quotes = lambda: (_ for _ in ()).throw(RuntimeError("sina failed"))

    with patch.object(config, "QUOTE_SNAPSHOT_PATH", str(tmp_path / "quotes.json")), patch.object(
        config,
        "QUOTE_SNAPSHOT_MIN_ROWS",
        1,
    ), patch.object(config, "ALLOW_SLOW_QUOTE_FALLBACK", True), patch.object(config, "TUSHARE_TOKEN", ""):
        provider._save_quote_snapshot(snapshot)
        quotes = provider.get_realtime_quotes()

    assert str(quotes.iloc[0]["code"]).zfill(6) == "600001"
    assert quotes.attrs.get("quote_timestamp") == "2026-07-09T15:00:00"
    assert provider.status.quotes_source == "本地快照"
    assert any("AKShare 行情失败" in error for error in provider.status.errors)
    assert not any("Tushare 行情失败" in error for error in provider.status.errors)


def test_provider_get_history_uses_local_market_data(tmp_path):
    raw = pd.DataFrame(
        {
            "trade_date": pd.date_range("2024-01-01", periods=35, freq="D").strftime("%Y%m%d"),
            "open": [10 + index * 0.1 for index in range(35)],
            "high": [10.5 + index * 0.1 for index in range(35)],
            "low": [9.8 + index * 0.1 for index in range(35)],
            "close": [10.2 + index * 0.1 for index in range(35)],
            "volume": [1000 + index for index in range(35)],
            "turnover": [10000000 + index * 10000 for index in range(35)],
            "pct_chg": [0.1 for _ in range(35)],
        }
    )
    with patch.object(config, "MARKET_DATA_DB_PATH", str(tmp_path / "market_data")), patch.object(
        config,
        "HISTORY_CACHE_PATH",
        str(tmp_path / "history.sqlite3"),
    ):
        DailyMarketDataStore(config.MARKET_DATA_DB_PATH).upsert_bars("600001", raw, raw)
        provider = MarketDataProvider()
        provider._fetch_akshare_history = lambda code, days: (_ for _ in ()).throw(
            AssertionError("network history should not be fetched")
        )

        history = provider.get_history("600001", days=30)
        prefetch = provider.prefetch_history(["600001"], days=30)

    assert len(history) == 30
    assert history.iloc[-1]["trade_date"] == "20240204"
    assert prefetch["local"] == 1
    assert prefetch["failed"] == 0


def test_provider_falls_back_to_akshare_quotes_when_enabled():
    provider = MarketDataProvider()
    provider._fetch_eastmoney_quotes = lambda: (_ for _ in ()).throw(RuntimeError("eastmoney failed"))
    provider._fetch_akshare_quotes = lambda: pd.DataFrame(
        [{"code": "600001", "name": "样本股份", "price": 12, "pct_chg": 3, "turnover": 90000000}]
    )

    with patch.object(config, "ALLOW_SLOW_QUOTE_FALLBACK", True):
        quotes = provider.get_realtime_quotes()

    assert quotes.iloc[0]["code"] == "600001"
    assert provider.status.quotes_source == "AKShare 东方财富"


def test_eastmoney_request_uses_proxy_environment_first():
    payload = {"data": {"diff": [{"f12": "600001"}]}}
    response = MagicMock()
    response.json.return_value = payload
    session = MagicMock()
    session.__enter__.return_value = session
    session.get.return_value = response

    with patch("stock_analyzer.providers.requests.Session", return_value=session):
        result = _request_eastmoney_page({"pn": "1"})

    assert result == payload
    assert session.trust_env
    session.get.assert_called_once()


def test_eastmoney_request_retries_without_proxy_environment():
    payload = {"data": {"diff": [{"f12": "600001"}]}}
    response = MagicMock()
    response.json.return_value = payload
    env_session = MagicMock()
    env_session.__enter__.return_value = env_session
    env_session.get.side_effect = RuntimeError("proxy failed")
    direct_session = MagicMock()
    direct_session.__enter__.return_value = direct_session
    direct_session.get.return_value = response

    with patch("stock_analyzer.providers.requests.Session", side_effect=[env_session, direct_session]):
        result = _request_eastmoney_page({"pn": "1"})

    assert result == payload
    assert env_session.trust_env
    assert not direct_session.trust_env


def test_health_endpoint_exposes_factor_coverage_alerts_in_provider_health(tmp_path):
    quotes = pd.DataFrame(
        [
            {"code": "600001", "name": "样本A", "price": 10, "pct_chg": 2, "turnover": 500000000},
            {"code": "600002", "name": "样本B", "price": 11, "pct_chg": 1, "turnover": 450000000},
            {"code": "600003", "name": "样本C", "price": 12, "pct_chg": 3, "turnover": 550000000},
        ]
    )

    with patch.object(config, "FACTOR_COVERAGE_ALERT_ZERO_RATIO", 0.30), patch(
        "stock_analyzer.providers.MarketDataProvider.get_realtime_quotes",
        return_value=quotes,
    ), patch(
        "stock_analyzer.providers.MarketDataProvider.health",
        return_value={"quotes_source": "测试行情"},
    ), app_patch_context(tmp_path) as app:
        response = app.test_client().get("/api/health")

    payload = response.get_json()
    alert_codes = {alert["code"] for alert in payload["health"]["alerts"]}
    assert response.status_code == 200
    assert payload["factor_coverage"]["degraded"]
    assert "alphalite_coverage_zero" in alert_codes
    assert "factor_coverage" in payload["health"]

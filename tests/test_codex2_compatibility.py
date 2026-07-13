from unittest.mock import patch

import pandas as pd

from stock_analyzer import config
from stock_analyzer.app import create_app
from stock_analyzer.scoring_core import compat
from stock_analyzer.strategy_validation import _load_execution_history
from stock_analyzer.providers import MarketDataProvider


def test_app_create_app_does_not_auto_start_background_workers_by_default():
    with patch.object(config, "WEB_BACKGROUND_WORKERS_ENABLED", False):
        with patch("stock_analyzer.app.AppServices.start_background_workers") as start_background_workers:
            create_app()
            start_background_workers.assert_not_called()


def test_app_create_app_does_not_auto_start_background_workers_even_when_enabled():
    with patch.object(config, "WEB_BACKGROUND_WORKERS_ENABLED", True):
        with patch("stock_analyzer.app.AppServices.start_background_workers") as start_background_workers:
            create_app()
            start_background_workers.assert_not_called()


def test_background_workers_can_be_started_explicitly_from_service():
    with patch.object(config, "WEB_BACKGROUND_WORKERS_ENABLED", True), patch.object(
        config,
        "VALIDATION_AUTO_UPDATE_ENABLED",
        False,
    ), patch.object(
        config,
        "VALIDATION_AUTO_SNAPSHOT_ENABLED",
        False,
    ):
        app = create_app()
        services = app.extensions["app_services"]
        with patch.object(services, "start_background_workers") as start_background_workers:
            services.start_background_workers()
            start_background_workers.assert_called_once()


def test_scoring_core_context_injection_is_immutable_and_overridable():
    result = compat.call_with_scoring_core_overrides(
        namespace={"metric": 1, "_internal": 3},
        baseline={"metric": 0},
        strategy_entrypoints={"_internal"},
        scoring_context={"metric": 9},
        callback=lambda scoring_context: scoring_context,
    )
    assert result["metric"] == 9
    try:
        result["metric"] = 10
    except Exception as exc:
        assert isinstance(exc, TypeError)
    else:
        raise AssertionError("scoring_context should be immutable")


def test_load_execution_history_prefers_get_execution_bars_raw_when_present():
    raw = pd.DataFrame([
        {"trade_date": "20240101", "price": 10.0, "open": 10.0},
    ])
    raw.attrs["price_adjustment_mode"] = "raw"
    legacy = pd.DataFrame([
        {"trade_date": "20240101", "price": 9.0, "open": 9.0},
    ])

    class Provider:
        def __init__(self):
            self.execution_called = False
            self.history_called = False

        def get_execution_bars_raw(self, code, days=180):
            self.execution_called = True
            return raw

        def get_history(self, code, days=180):
            self.history_called = True
            return legacy

    provider = Provider()
    history, mode, source = _load_execution_history(provider, "600001", days=180)

    assert provider.execution_called is True
    assert provider.history_called is False
    assert source == "provider_raw_execution_history"
    assert mode == "raw"
    assert not history.empty


def test_marketr_snapshot_api_returns_copy_and_metadata():
    source = pd.DataFrame(
        [
            {
                "code": "600001",
                "name": "样本",
                "price": 10.0,
            }
        ]
    )

    with patch.object(MarketDataProvider, "get_realtime_quotes", return_value=source):
        provider = MarketDataProvider()
        snapshot = provider.get_intraday_snapshot(as_of="2024-01-01T15:00:00")

    assert snapshot.attrs.get("price_adjustment_mode") == "snapshot"
    assert snapshot.attrs.get("price_data_source") == "realtime"
    assert snapshot.attrs.get("snapshot_as_of") == "2024-01-01T15:00:00"

    snapshot.loc[0, "price"] = 9.9
    assert source.iloc[0]["price"] == 10.0

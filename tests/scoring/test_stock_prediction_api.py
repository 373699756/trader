from unittest.mock import patch

import pandas as pd

from helpers import app_patch_context


def _live_quotes():
    return pd.DataFrame(
        [
            {
                "code": "600001",
                "name": "芯片设备",
                "price": 12,
                "pct_chg": 3.2,
                "speed": 1.5,
                "volume_ratio": 1.8,
                "turnover_rate": 5,
                "turnover": 700000000,
                "industry": "半导体设备",
                "sixty_day_pct": 18,
                "ytd_pct": 32,
                "amplitude": 5,
            },
            {
                "code": "600002",
                "name": "普通样本",
                "price": 9,
                "pct_chg": 0.8,
                "speed": 0.2,
                "volume_ratio": 1.1,
                "turnover_rate": 2,
                "turnover": 120000000,
                "industry": "银行",
                "sixty_day_pct": 6,
                "ytd_pct": 8,
                "amplitude": 3,
            },
        ]
    )


def test_stock_prediction_endpoint_returns_strategy_direction(tmp_path):
    with patch(
        "stock_analyzer.providers.MarketDataProvider.get_realtime_quotes",
        return_value=_live_quotes(),
    ), app_patch_context(tmp_path) as app:
        response = app.test_client().get("/api/stock-prediction/600001")

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["ok"]
    assert payload["code"] == "600001"
    assert payload["prediction"]["direction"] in {"up", "neutral", "down"}
    assert payload["horizons"]["short"]["prediction"]["direction"] in {"up", "neutral", "down"}
    assert payload["horizons"]["long"]["prediction"]["direction"] in {"up", "neutral", "down"}
    assert len(payload["strategy_hits"]) >= 1
    assert "disclaimer" in payload


def test_stock_prediction_endpoint_uses_history_when_realtime_quote_missing(tmp_path):
    history = pd.DataFrame(
        [
            {"trade_date": "2026-01-02", "price": 10.0, "volume": 1000000, "turnover": 80000000},
            {"trade_date": "2026-01-03", "price": 10.5, "volume": 1200000, "turnover": 90000000},
            {"trade_date": "2026-01-04", "price": 10.2, "volume": 1100000, "turnover": 85000000},
        ]
    )
    with patch(
        "stock_analyzer.providers.MarketDataProvider.get_realtime_quotes",
        return_value=_live_quotes(),
    ), patch(
        "stock_analyzer.providers.MarketDataProvider.get_history",
        return_value=history,
    ), app_patch_context(tmp_path) as app:
        response = app.test_client().get("/api/stock-prediction/600999")

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["ok"]
    assert payload["filtered"]
    assert payload["data_source"] == "历史行情兜底"
    assert "实时行情源未返回" in "；".join(payload["risk_flags"])
    assert payload["price"] > 0


def test_stock_prediction_endpoint_returns_risk_diagnosis_for_filtered_stock(tmp_path):
    quotes = pd.DataFrame(
        [
            {
                "code": "600003",
                "name": "低流动样本",
                "price": 5,
                "pct_chg": 1.0,
                "volume_ratio": 0.4,
                "turnover": 1000,
                "sixty_day_pct": -12,
                "ytd_pct": -18,
                "industry": "银行",
            }
        ]
    )

    with patch(
        "stock_analyzer.providers.MarketDataProvider.get_realtime_quotes",
        return_value=quotes,
    ), app_patch_context(tmp_path, ENABLE_HISTORY_FACTORS=False) as app:
        response = app.test_client().get("/api/stock-prediction/600003")

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["ok"]
    assert payload["filtered"]
    assert payload["prediction"]["direction"] == "down"
    assert payload["horizons"]["short"]["prediction"]["direction"] == "down"
    assert payload["horizons"]["long"]["prediction"]["direction"] == "down"
    assert "成交额不足" in "；".join(payload["risk_flags"])
    assert payload["strategy_hits"] == []


def test_stock_prediction_endpoint_returns_diagnosis_when_all_quotes_missing(tmp_path):
    def fail_quotes(self):
        raise RuntimeError("实时源不可用")

    with patch(
        "stock_analyzer.providers.MarketDataProvider.get_realtime_quotes",
        fail_quotes,
    ), patch(
        "stock_analyzer.providers.MarketDataProvider.get_history",
        return_value=pd.DataFrame(),
    ), app_patch_context(tmp_path, ENABLE_HISTORY_FACTORS=False) as app:
        response = app.test_client().get("/api/stock-prediction/600999")

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["ok"]
    assert payload["filtered"]
    assert payload["data_source"] == "无可用行情"
    assert payload["prediction"]["direction"] == "down"
    assert "历史行情也不可用" in "；".join(payload["risk_flags"])
    assert payload["strategy_hits"] == []

import time
from unittest.mock import patch

import pandas as pd

from helpers import app_patch_context
from stock_analyzer import config
from stock_analyzer.recommendation_snapshot import load_recommendation_snapshot, save_recommendation_snapshot


def _live_quotes(count=2):
    rows = [
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
    return pd.DataFrame(rows[:count])


def test_recommendations_endpoint_returns_market_regime(tmp_path):
    with patch.object(config, "DEFAULT_TOP_N", 10), patch(
        "stock_analyzer.providers.MarketDataProvider.get_realtime_quotes",
        return_value=_live_quotes(),
    ), patch(
        "stock_analyzer.services.app_services.AppServices._schedule_snapshot_save",
        return_value=None,
    ), patch(
        "stock_analyzer.services.app_services.threading.Thread.start",
        return_value=None,
    ), app_patch_context(tmp_path) as app:
        response = app.test_client().get("/api/recommendations?top_n=10&market=all")

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["ok"]
    assert payload["meta"]["market_regime"]["level"] == "risk_on"
    assert payload["recommendations"]["short_term"][0]["code"] == "600001"
    assert "serenity_profile" in payload["recommendations"]["short_term"][0]
    assert "agent_committee" in payload["recommendations"]["short_term"][0]


def test_latest_recommendations_rejects_snapshot_for_other_market(tmp_path):
    snapshot_path = tmp_path / "latest.json"
    save_recommendation_snapshot(
        str(snapshot_path),
        {
            "ok": True,
            "recommendations": {"short_term": [{"code": "600001"}]},
            "meta": {"market_filter": "all", "top_n": 18},
        },
    )

    with patch.object(config, "DEFAULT_TOP_N", 18), patch.object(
        config,
        "RECOMMENDATION_MAX_TOP_N",
        18,
    ), app_patch_context(tmp_path, RECOMMENDATION_SNAPSHOT_PATH=str(snapshot_path)) as app:
        client = app.test_client()
        mismatch = client.get("/api/recommendations/latest?market=chinext&top_n=18")
        matched = client.get("/api/recommendations/latest?market=all&top_n=18")

    assert mismatch.status_code == 404
    assert mismatch.get_json()["snapshot"]["status"] == "market_mismatch"
    assert matched.status_code == 200
    assert matched.get_json()["recommendations"]["short_term"][0]["code"] == "600001"


def test_recommendation_snapshot_rejects_market_and_top_n_mismatch(tmp_path):
    payload = {
        "ok": True,
        "recommendations": {"short_term": [{"code": "600001"}]},
        "meta": {"market_filter": "all", "top_n": 18},
    }
    snapshot_path = tmp_path / "latest.json"
    save_recommendation_snapshot(str(snapshot_path), payload)

    market_mismatch = load_recommendation_snapshot(str(snapshot_path), expected_market="chinext", expected_top_n=18)
    top_n_mismatch = load_recommendation_snapshot(str(snapshot_path), expected_market="all", expected_top_n=10)
    matched = load_recommendation_snapshot(str(snapshot_path), expected_market="all", expected_top_n=18)

    assert market_mismatch["status"] == "market_mismatch"
    assert top_n_mismatch["status"] == "top_n_mismatch"
    assert matched["ok"]


def test_recommendations_prefers_cached_snapshot_before_live_recompute(tmp_path):
    payload = {
        "ok": True,
        "data": [{"code": "600001", "name": "快照样本"}],
        "recommendations": {"short_term": [{"code": "600001", "name": "快照样本"}]},
        "meta": {"market_filter": "all", "top_n": 18},
    }
    snapshot_path = tmp_path / "latest.json"
    save_recommendation_snapshot(str(snapshot_path), payload)

    with patch.object(config, "DEFAULT_TOP_N", 18), patch.object(
        config,
        "RECOMMENDATION_MAX_TOP_N",
        18,
    ), patch.object(config, "REFRESH_SECONDS", 30), patch(
        "stock_analyzer.providers.MarketDataProvider.get_realtime_quotes",
        side_effect=AssertionError("should not recompute live quotes when snapshot is fresh"),
    ), app_patch_context(tmp_path, RECOMMENDATION_SNAPSHOT_PATH=str(snapshot_path)) as app:
        response = app.test_client().get("/api/recommendations?market=all&top_n=18")

    body = response.get_json()
    assert response.status_code == 200
    assert body["ok"]
    assert body["recommendations"]["short_term"][0]["code"] == "600001"
    assert body["snapshot"]["source"] == "disk_snapshot"


def test_recommendations_does_not_save_snapshot_synchronously(tmp_path):
    with patch(
        "stock_analyzer.providers.MarketDataProvider.get_realtime_quotes",
        return_value=_live_quotes(count=1),
    ), patch(
        "stock_analyzer.app_container.save_recommendation_snapshot",
        side_effect=AssertionError("snapshot save should be async"),
    ), patch(
        "stock_analyzer.app_container.threading.Thread.start",
        return_value=None,
    ), patch(
        "stock_analyzer.services.app_services.threading.Thread.start",
        return_value=None,
    ), app_patch_context(tmp_path) as app:
        response = app.test_client().get("/api/recommendations?top_n=10&market=all")

    assert response.status_code == 200
    assert response.get_json()["ok"]


def test_horizon_refresh_failure_is_cached_without_thread_traceback(tmp_path):
    def fail_quotes(self):
        raise RuntimeError("东方财富直连行情失败: disconnected")

    with patch(
        "stock_analyzer.providers.MarketDataProvider.get_realtime_quotes",
        fail_quotes,
    ), patch(
        "stock_analyzer.providers.MarketDataProvider.health",
        return_value={"quotes_source": "测试行情", "errors": []},
    ), app_patch_context(tmp_path) as app:
        client = app.test_client()
        first = client.get("/api/tomorrow-picks?top_n=18&market=all")
        payload = {}
        for _ in range(20):
            time.sleep(0.05)
            second = client.get("/api/tomorrow-picks?top_n=18&market=all")
            payload = second.get_json()
            if payload.get("meta", {}).get("fallback") == "live_refresh_failed":
                break

    assert first.status_code == 200
    assert payload["meta"]["fallback"] == "live_refresh_failed"
    assert not payload["ok"]
    assert "东方财富直连行情失败" in payload["error"]


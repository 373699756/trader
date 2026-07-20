from __future__ import annotations

import threading

from trader.web import create_app


def test_create_app_has_no_thread_or_filesystem_side_effects(tmp_path, monkeypatch) -> None:
    started: list[str] = []
    original_start = threading.Thread.start

    def record_start(thread: threading.Thread) -> None:
        started.append(thread.name)
        original_start(thread)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(threading.Thread, "start", record_start)

    app = create_app()

    assert app is not None
    assert started == []
    assert list(tmp_path.iterdir()) == []
    response = app.test_client().get("/api/status")
    assert response.status_code == 200
    assert response.get_json()["status"] == "not_ready"


def test_dashboard_uses_packaged_v2_assets() -> None:
    app = create_app()
    client = app.test_client()

    response = client.get("/")
    page = response.get_data(as_text=True)
    dashboard = client.get("/static/dashboard.js").get_data(as_text=True)

    assert response.status_code == 200
    assert "A股策略看板" in page
    assert "策略验证" not in page
    assert "/static/dashboard.css?v=3" in page
    assert "/static/render.js?v=5" in page
    assert "/static/dashboard.js?v=6" in page
    assert "payloads: new Map()" in dashboard
    assert "inflight: new Map()" in dashboard
    assert "prefetchStrategies();" in dashboard
    assert 'Promise.all([loadDates(), loadRecommendations("strategy")])' in dashboard
    assert "displayableCachedPayload" in dashboard
    assert "cacheIdentityValid" in dashboard
    assert "上一交易日快照" in dashboard
    assert "patchLiveRows" in dashboard
    assert "currentRow.replaceWith" in dashboard
    assert "payload.strategy !== strategy" in dashboard
    assert "CACHE_MAX_AGE_MS = 30000" in dashboard
    assert 'addEventListener("live_overlay"' in dashboard
    assert client.get("/static/dashboard.css").status_code == 200
    assert client.get("/static/lucide.svg").status_code == 200
    renderer_response = client.get("/static/render.js")
    renderer = renderer_response.get_data(as_text=True)
    assert renderer_response.status_code == 200
    assert 'scores.deepseek_score == null ? "未复核"' in renderer
    assert 'api_key_missing: "不可用：未配置 API 密钥"' in renderer
    assert 'return "拒绝：响应未通过结构化校验"' in renderer
    assert 'section("缺失字段"' in renderer
    assert "实际 ${escapeHtml(actual)}" in renderer
    assert "阈值 ${escapeHtml(risk.threshold" in renderer
    assert "证据时间 ${escapeHtml(formatDateTime(risk.observed_at))}" in renderer
    assert "anchor_to_now_pct" in renderer
    assert 'section("权重"' in renderer
    assert 'section("分位与截尾"' in renderer
    assert "risk.assessment" in renderer
    assert "row," in renderer
    assert client.get("/static/dashboard.js").status_code == 200

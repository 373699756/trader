from __future__ import annotations

import shutil
import subprocess
import threading
from pathlib import Path

import pytest

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
    assert "股票详情" in page
    assert "策略验证" not in page
    assert "/static/dashboard.css?v=5" in page
    assert "/static/render.js?v=8" in page
    assert "/static/dashboard.js?v=15" in page
    assert 'data-view="live"' in page
    assert "临时实时" in page
    assert 'class="runtime-error"' in page
    assert "payloads: new Map()" in dashboard
    assert "inflight: new Map()" in dashboard
    assert "prefetchStrategies();" in dashboard
    assert 'Promise.all([loadDates(), loadRecommendations("strategy")])' in dashboard
    assert "displayableCachedPayload" in dashboard
    assert "cacheIdentityValid" in dashboard
    assert "上一交易日快照" not in dashboard
    assert "previous_trade_date_snapshot" not in dashboard
    assert "patchLiveRows" in dashboard
    assert "currentRow.replaceWith" in dashboard
    assert "patch_schema_version === 2" in dashboard
    assert "base_projection_version" in dashboard
    assert "removed_codes" in dashboard
    assert "rowIdentity" in dashboard
    assert "overlay_projection_mismatch" in dashboard
    assert "payload.strategy !== strategy" in dashboard
    assert "CACHE_MAX_AGE_MS = 30000" in dashboard
    assert "budget.available === false" in dashboard
    assert '? "不可用"' in dashboard
    assert 'addEventListener("overlay_patch"' in dashboard
    assert 'addEventListener("recommendation_patch"' in dashboard
    assert "applyRecommendationPatch" in dashboard
    assert "recommendationPatchDecision" in dashboard
    assert "overlayPatchDecision" in dashboard
    assert "requestRecommendationResync" in dashboard
    assert "TraderDashboardDiagnostics" in dashboard
    assert "browserErrors" in dashboard
    assert "reconcileRecommendationIdentity(payload)" in dashboard
    assert 'loadRecommendations("status_identity")' in dashboard
    assert 'query.set("view", "live")' in dashboard
    assert "HISTORY_REFRESH_MS = 3000" in dashboard
    assert 'close_fallback: "收盘补算"' in dashboard
    assert 'payload.phase === "close_fallback"' in dashboard
    stylesheet_response = client.get("/static/dashboard.css")
    stylesheet = stylesheet_response.get_data(as_text=True)
    assert stylesheet_response.status_code == 200
    assert '@import url("./dashboard_base.css");' in stylesheet
    assert '@import url("./dashboard_components.css");' in stylesheet
    assert '@import url("./dashboard_responsive.css");' in stylesheet

    base_response = client.get("/static/dashboard_base.css")
    components_response = client.get("/static/dashboard_components.css")
    responsive_response = client.get("/static/dashboard_responsive.css")
    assert base_response.status_code == 200
    assert components_response.status_code == 200
    assert responsive_response.status_code == 200
    assert ".runtime-error" in base_response.get_data(as_text=True)
    assert "overflow-wrap: anywhere" in components_response.get_data(as_text=True)
    assert client.get("/static/lucide.svg").status_code == 200
    renderer_response = client.get("/static/render.js")
    renderer = renderer_response.get_data(as_text=True)
    assert renderer_response.status_code == 200
    assert 'section("推荐结论"' in renderer
    assert 'section("核心行情"' in renderer
    assert 'section("评分与风险"' in renderer
    assert 'api_key_missing: "不可用：未配置 API 密钥"' in renderer
    assert 'return "拒绝：响应未通过结构化校验"' in renderer
    assert "部分核心行情暂缺" in renderer
    assert "模型评分未参与最终分，当前使用本地模式" in renderer
    assert "anchor_to_now_pct" in renderer
    assert "risk.assessment" in renderer
    assert "RISK_SEVERITY_LABELS" in renderer
    assert 'section("缺失字段"' not in renderer
    assert 'section("权重"' not in renderer
    assert "板块与交易规则" not in renderer
    assert "多源合并" not in renderer
    assert 'section("DeepSeek 审计"' not in renderer
    assert "review.challenger_actual_model" not in renderer
    assert "review.prompt_cache_hit_tokens" not in renderer
    assert "row," in renderer
    assert client.get("/static/dashboard.js").status_code == 200


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required for the dashboard state contract")
def test_dashboard_patch_state_machine_contract() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            "node",
            str(repository_root / "tests" / "js" / "test_dashboard_d4.js"),
            str(repository_root / "src" / "trader" / "web" / "static" / "dashboard.js"),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert result.stdout.strip() == "dashboard D4 state contract passed"

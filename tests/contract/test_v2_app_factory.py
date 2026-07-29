from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from trader.web import create_app
from trader.web.static_assets import WEB_ASSET_REVISION

PROJECT_ROOT = Path(__file__).resolve().parents[2]


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
    selection = client.get("/static/selection.js").get_data(as_text=True)
    dashboard_formatters = client.get("/static/dashboard_formatters.js").get_data(as_text=True)
    dashboard_patches = client.get("/static/dashboard_patches.js").get_data(as_text=True)

    assert response.status_code == 200
    assert "A股策略看板" in page
    assert "股票详情" in page
    assert "策略验证" not in page
    assert "?v=" not in page
    assert page.count(f"?rev={WEB_ASSET_REVISION}") == 10
    assert f"/static/dashboard_base.css?rev={WEB_ASSET_REVISION}" in page
    assert f"/static/dashboard_components.css?rev={WEB_ASSET_REVISION}" in page
    assert f"/static/dashboard_responsive.css?rev={WEB_ASSET_REVISION}" in page
    assert f"/static/render.js?rev={WEB_ASSET_REVISION}" in page
    assert f"/static/selection.js?rev={WEB_ASSET_REVISION}" in page
    assert f"/static/long_watchlist_data.js?rev={WEB_ASSET_REVISION}" in page
    assert f"/static/long_groups.js?rev={WEB_ASSET_REVISION}" in page
    assert f"/static/dashboard_formatters.js?rev={WEB_ASSET_REVISION}" in page
    assert f"/static/dashboard_patches.js?rev={WEB_ASSET_REVISION}" in page
    assert f"/static/dashboard.js?rev={WEB_ASSET_REVISION}" in page
    assert 'id="currentViewStatus"' not in page
    assert 'class="current-view-status"' not in page
    assert 'id="strategyDescription"' in page
    assert 'id="topScore"' in page
    assert 'id="modelReview"' in page
    assert 'id="dataQuality"' in page
    assert 'id="routeHealth"' not in page
    assert 'id="strategyVersion"' not in page
    assert 'id="freezeStatus"' not in page
    assert 'id="observationTable"' in page
    assert 'id="observationPool"' in page
    assert "不可执行，仅供观察" in page
    assert 'data-view="live"' not in page
    assert "正式当前" not in page
    assert "临时实时" not in page
    assert 'class="runtime-error runtime-message"' in page
    assert 'class="runtime-messages"' in page
    assert 'id="noticeText"' in page
    assert 'id="recommendation-layout"' in page
    assert 'id="long-sidebar"' in page
    assert page.index('class="runtime-messages"') < page.index('class="control-band"')
    assert page.index('class="summary-band"') < page.index('class="control-band"') < page.index('class="table-region"')
    assert 'id="tableTitle"' not in page
    assert "payloads: new Map()" in dashboard
    assert "inflight: new Map()" in dashboard
    assert "prefetchStrategies();" in dashboard
    assert "resolveStrategyDate" in dashboard
    assert "renderMissingHistoricalDate" in dashboard
    assert "selectedDateAvailability" in dashboard
    assert 'state.date ? "18" : "12"' in dashboard
    assert "setLongLayout" in dashboard
    assert 'payload.strategy === "long" && payload.status === "ready" && !historical' in dashboard
    assert "tableDefinition(payload)" in dashboard
    assert "displayableCachedPayload" in dashboard
    assert "cacheIdentityValid" in dashboard
    assert "state.projectionVersion = projectionVersion(payload)" not in dashboard
    assert "上一交易日快照" not in dashboard
    assert "previous_trade_date_snapshot" not in dashboard
    assert "patchLiveRows" in dashboard
    assert "currentRow.replaceWith" in dashboard
    assert "patch_schema_version === 2" in dashboard_patches
    assert "base_projection_version" in dashboard_patches
    assert "removed_codes" in dashboard
    assert "rowIdentity" in dashboard
    assert "overlay_projection_mismatch" in dashboard_patches
    assert "patch.strategy !== strategy" in dashboard_patches
    assert "CACHE_MAX_AGE_MS = 30000" in dashboard
    assert "budget.available === false" in dashboard
    assert '? "不可用"' in dashboard
    assert 'addEventListener("overlay_patch"' in dashboard
    assert 'addEventListener("recommendation_patch"' in dashboard
    assert "applyRecommendationPatch" in dashboard
    assert "recommendationPatchDecision" in dashboard
    assert "overlayPatchDecision" in dashboard
    assert "requestRecommendationResync" in dashboard
    assert "fallbackDashboardPatches" in dashboard
    assert "dependency_missing:TraderDashboardPatches" in dashboard
    assert "TraderDashboardDiagnostics" in dashboard
    assert "browserErrors" in dashboard
    assert "reconcileRecommendationIdentity(payload)" in dashboard
    assert 'loadRecommendations("status_identity")' in dashboard
    assert 'view: "current"' in dashboard
    assert 'query.set("view", view)' in dashboard
    assert "recommendationSummary" in selection
    assert "HISTORY_REFRESH_MS = 3000" in dashboard
    assert 'close_fallback: "收盘恢复中"' in dashboard_formatters
    assert 'continuous: "连续交易"' in dashboard_formatters
    assert 'unavailable: "暂不可用"' in dashboard_formatters
    assert "sourceLabel(market.active_source)" in dashboard
    assert "sourceLabel(firstVisible.source)" in dashboard
    assert "sourceLabel(first.source)" in dashboard
    assert 'payload.phase === "close_fallback"' in dashboard_patches
    assert "11:20 已冻结 · 名单与评分不变" in dashboard_patches
    assert "行情已过期，当前报价仅供观察" in dashboard_patches
    assert "实时草稿" not in dashboard
    assert "实时快照" in dashboard_patches
    assert "流水线已启动，当前策略尚无可用快照" not in dashboard
    assert "当前策略尚未发布快照" not in dashboard
    assert "最高评分" in page
    assert "模型复核" in page
    assert "数据状态" in page
    assert 'id="long-panel-title">卡脖子行业<' in page
    assert 'id="longStockHeader"' in page
    assert ">重点股票行情<" in page
    stylesheet_response = client.get("/static/dashboard.css")
    stylesheet = stylesheet_response.get_data(as_text=True)
    assert stylesheet_response.status_code == 200
    assert "?v=" not in stylesheet
    assert '@import url("./dashboard_base.css");' in stylesheet
    assert '@import url("./dashboard_components.css");' in stylesheet
    assert '@import url("./dashboard_responsive.css");' in stylesheet

    base_response = client.get("/static/dashboard_base.css")
    components_response = client.get("/static/dashboard_components.css")
    responsive_response = client.get("/static/dashboard_responsive.css")
    assert base_response.status_code == 200
    assert components_response.status_code == 200
    assert responsive_response.status_code == 200
    base_styles = base_response.get_data(as_text=True)
    assert ".runtime-error" in base_styles
    assert "--runtime-message-height: 52px" in base_styles
    assert "height: var(--runtime-message-height)" in base_styles
    assert "overflow-y: auto" in base_styles
    assert "overflow-wrap: anywhere" in components_response.get_data(as_text=True)
    assert client.get("/static/lucide.svg").status_code == 200
    assert client.get("/static/selection.js").status_code == 200
    long_groups_response = client.get("/static/long_groups.js")
    assert long_groups_response.status_code == 200
    assert "displayPayload" in long_groups_response.get_data(as_text=True)
    long_watchlist_response = client.get("/static/long_watchlist_data.js")
    assert long_watchlist_response.status_code == 200
    assert "TraderLongWatchlistData" in long_watchlist_response.get_data(as_text=True)
    formatters_response = client.get("/static/dashboard_formatters.js")
    assert formatters_response.status_code == 200
    assert "window.TraderDashboardFormatters" in formatters_response.get_data(as_text=True)
    patches_response = client.get("/static/dashboard_patches.js")
    assert patches_response.status_code == 200
    assert "window.TraderDashboardPatches" in patches_response.get_data(as_text=True)
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
    assert "longTable" in renderer
    assert "tableColumnCount" in renderer
    assert "行情来源 / 时间" in renderer
    assert client.get("/static/dashboard.js").status_code == 200


def test_active_version_labels_are_readable_and_governed() -> None:
    strategy = json.loads((PROJECT_ROOT / "config" / "v2" / "strategy.json").read_text(encoding="utf-8"))
    runtime = json.loads((PROJECT_ROOT / "config" / "v2" / "runtime.json").read_text(encoding="utf-8"))
    watchlist = json.loads((PROJECT_ROOT / "config" / "v2" / "long_watchlist.json").read_text(encoding="utf-8"))
    replay = (PROJECT_ROOT / "src" / "trader" / "application" / "recommendation_replay.py").read_text(encoding="utf-8")

    active_labels = (
        strategy["strategy_version"],
        strategy["board_policy_version"],
        strategy["fusion"]["version"],
        runtime["market_data"]["cache_policy"]["policy_version"],
        watchlist["watchlist_version"],
        re.search(r'^REPLAY_ALGORITHM_VERSION = "([^"]+)"$', replay, re.MULTILINE).group(1),
    )

    assert active_labels == (
        "strategy_review30_top6_observe6_2026_07",
        "board_policy_score_first_2026_07",
        "fusion_local68_deepseek32",
        "market_cache_p1_p6",
        "long_watchlist_document_merge_2026_07",
        "engine_review28_2026_07",
    )
    assert all(not re.search(r"(^|_)v\d+($|_)", label) for label in active_labels)
    assert "LEGACY_REPLAY_ALGORITHM_VERSION" in replay
    assert "V17_REPLAY_ALGORITHM_VERSION" in replay


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

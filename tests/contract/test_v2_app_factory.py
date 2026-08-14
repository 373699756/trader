from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from trader.web import create_app
from trader.web.static_assets import WEB_ASSET_REVISION

ROOT = Path(__file__).resolve().parents[2]


def test_create_app_has_no_thread_or_filesystem_side_effects(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    app = create_app()
    assert list(tmp_path.iterdir()) == []
    assert app.test_client().get("/").status_code == 200
    assert app.test_client().get("/api/v2/status").status_code == 503
    assert app.test_client().get("/api/status").status_code == 404


def test_dashboard_uses_only_packaged_v2_assets_and_fixed_long_groups() -> None:
    client = create_app().test_client()
    page = client.get("/").get_data(as_text=True)
    dashboard = client.get("/static/dashboard.js").get_data(as_text=True)
    groups = client.get("/static/long_groups.js").get_data(as_text=True)

    assert page.count(f"?rev={WEB_ASSET_REVISION}") == 11
    assert 'id="long-panel-title">卡脖子行业<' in page
    assert 'data-scope="future_growth"' in page
    assert 'data-scope="low_price_potential"' in page
    assert page.count('class="summary-item"') == 5
    assert 'id="healthBadge"' in page
    assert 'id="errorDetailsButton"' in page
    assert 'id="errorDrawer"' in page
    assert 'id="quoteCoverageStatus"' in page
    assert 'id="quoteCoverageMeta"' in page
    assert 'id="funnelStatus"' in page
    assert 'id="snapshotDate"' in page
    assert "quote_status: quote.status" in dashboard
    assert "/api/v2/decisions/" in dashboard
    assert "/api/recommendations/" not in dashboard
    assert "卡脖子行业" in groups
    assert "高成长赛道" in groups
    assert "低价潜力股" in groups
    assert "staticFallbackPayload" in groups
    assert "longGroups.staticFallbackPayload" in dashboard
    assert 'setNotice("实时行情暂不可用，固定长期名单仍可查看", "warn")' in dashboard
    assert client.get("/static/long_watchlist_data.js").status_code == 200
    assert client.get("/static/render.js").status_code == 200


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_dashboard_state_contract() -> None:
    result = subprocess.run(
        ["node", str(ROOT / "tests/js/test_dashboard_d4.js"), str(ROOT / "src/trader/web/static/dashboard.js")],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr or result.stdout

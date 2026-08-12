from __future__ import annotations

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

    assert started == []
    assert list(tmp_path.iterdir()) == []
    assert app.test_client().get("/").status_code == 200
    assert app.test_client().get("/api/v2/status").status_code == 503
    assert app.test_client().get("/api/status").status_code == 404


def test_root_uses_only_unified_v2_dashboard_assets() -> None:
    client = create_app().test_client()

    page = client.get("/").get_data(as_text=True)

    assert "A股 V2 决策工作台" in page
    assert page.count(f"?rev={WEB_ASSET_REVISION}") == 5
    assert f"/static/dashboard.css?rev={WEB_ASSET_REVISION}" in page
    assert f"/static/render.js?rev={WEB_ASSET_REVISION}" in page
    assert f"/static/long_watchlist_data.js?rev={WEB_ASSET_REVISION}" in page
    assert f"/static/long_groups.js?rev={WEB_ASSET_REVISION}" in page
    assert f"/static/dashboard.js?rev={WEB_ASSET_REVISION}" in page
    assert "dashboard_patches.js" not in page
    assert "tomorrow_v2.js" not in page
    assert "/api/v2/decisions/" in client.get("/static/dashboard.js").get_data(as_text=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required for JavaScript syntax validation")
def test_unified_dashboard_javascript_parses() -> None:
    result = subprocess.run(
        ["node", "--check", str(PROJECT_ROOT / "src/trader/web/static/dashboard.js")],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr

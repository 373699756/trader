from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_root_dashboard_is_one_unified_v2_workbench() -> None:
    template = (ROOT / "src/trader/web/templates/index.html").read_text(encoding="utf-8")
    dashboard = (ROOT / "src/trader/web/static/dashboard.js").read_text(encoding="utf-8")

    for token in (
        'id="strategyTabs"',
        'data-strategy="today"',
        'data-strategy="tomorrow"',
        'data-strategy="d25"',
        'data-strategy="long"',
        'id="dataAge"',
        'id="coverage"',
        'id="funnel"',
        'id="budget"',
        'id="freeze"',
        'id="degraded"',
        'id="decisionTable"',
    ):
        assert token in template
    assert "dashboard_patches.js" not in template
    assert "tomorrow_v2.js" not in template
    assert "/api/v2/decisions/${state.strategy}" in dashboard
    assert 'new EventSource("/api/v2/events")' in dashboard
    assert "TraderV2Diagnostics" in dashboard


def test_unified_dashboard_layout_has_desktop_containment() -> None:
    css = (ROOT / "src/trader/web/static/dashboard.css").read_text(encoding="utf-8")

    assert "min-width: 1180px" in css
    assert "grid-template-columns: repeat(6, minmax(0, 1fr))" in css
    assert "grid-template-columns: minmax(0, 1fr) 280px" in css
    assert "overflow: auto" in css
    assert "max-height: calc(100vh - 360px)" in css


def test_packaged_long_watchlist_matches_runtime_configuration() -> None:
    config_source = (ROOT / "config/v2/long_watchlist.json").read_text(encoding="utf-8")
    config = json.loads(config_source)
    source = (ROOT / "src/trader/web/static/long_watchlist_data.js").read_text(encoding="utf-8").strip()
    prefix = '(function(){"use strict";window.TraderLongWatchlistData=Object.freeze('
    suffix = ");})();"

    assert "stock_analyzer/" not in config_source
    assert source.startswith(prefix)
    assert source.rstrip().endswith(suffix)
    packaged = json.loads(source[len(prefix) : -len(suffix)])
    assert packaged == config
    assert any(len(group["sections"]) > 1 for group in config["groups"] if group["category"] == "chokepoint")
    grouped_codes = [code for group in config["groups"] for code in group["codes"]]
    assert len(grouped_codes) == len(set(grouped_codes))
    assert set(grouped_codes) == {item["code"] for item in config["items"]}


def test_packaged_long_watchlist_is_deterministically_generated() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/generate_long_watchlist_asset.py"), "--check"],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr

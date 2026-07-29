from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_dashboard_separates_current_formal_and_observation_tables() -> None:
    template = (ROOT / "src/trader/web/templates/index.html").read_text(encoding="utf-8")
    dashboard = (ROOT / "src/trader/web/static/dashboard.js").read_text(encoding="utf-8")
    patches = (ROOT / "src/trader/web/static/dashboard_patches.js").read_text(encoding="utf-8")
    selection = (ROOT / "src/trader/web/static/selection.js").read_text(encoding="utf-8")

    assert 'id="recommendationTable"' in template
    assert 'id="recommendation-layout"' in template
    assert 'id="long-sidebar"' in template
    assert 'id="longScopeTabs"' in template
    assert 'id="long-panel-title"' in template
    assert 'id="longIndustryTabs"' in template
    assert 'id="longStockHeader"' in template
    description_end = template.index("</p>", template.index('id="strategyDescription"'))
    strategy_choice_end = template.index("</div>", description_end)
    assert description_end < template.index('id="longScopeTabs"') < strategy_choice_end
    assert template.index('id="longScopeTabs"') < template.index('id="recommendation-layout"')
    assert template.index('id="long-sidebar"') < template.index('id="longStockHeader"')
    assert 'data-scope="low_price_potential"' in template
    assert "web_asset('selection.js')" in template
    assert "?v=" not in template
    assert 'id="tableTitle"' not in template
    assert 'id="observationTable"' in template
    assert 'id="observationPool"' in template
    assert 'id="observationCount"' in template
    assert "不可执行，仅供观察" in template
    assert "最高评分" in patches
    assert "低于观察门槛" in patches
    assert "当前没有达到正式推荐条件的股票" in patches
    assert "长期策略当前尚无可用数据" in dashboard
    assert "当前暂无可用荐股数据" in dashboard
    assert "当前策略尚未发布快照" not in dashboard
    assert "visibleRecommendations(payload)" in dashboard
    assert 'item.action === "executable"' in selection
    assert "observationRecommendations(payload)" in dashboard
    assert "observation_floor" in dashboard
    assert "观察门槛 = 正式门槛" in dashboard
    assert 'item.action === "observe"' in selection
    assert 'payload.strategy === "long"' in dashboard
    assert 'setLongControls(nextStrategy === "long")' in dashboard
    assert 'setLongControls(state.strategy === "long")' in dashboard
    assert "els.longScopeTabs.hidden = !enabled" in dashboard
    assert 'state.date ? "18" : "12"' in dashboard
    assert "tableDefinition(payload)" in dashboard
    assert 'payload.phase === "close_fallback"' in selection
    assert "observationPool.hidden" in dashboard
    assert 'score_status === "not_applicable"' in dashboard


def test_web_schema_exposes_additive_downside_projection() -> None:
    schema = (ROOT / "src/trader/web/schemas.py").read_text(encoding="utf-8")

    assert '"setup_type"' in schema
    assert '"downside"' in schema


def test_long_dashboard_uses_left_group_sidebar() -> None:
    components = (ROOT / "src/trader/web/static/dashboard_components.css").read_text(encoding="utf-8")

    assert ".recommendation-layout.is-long" in components
    assert "grid-template-columns: 236px minmax(0, 1fr)" in components
    assert "grid-template-columns: repeat(3, minmax(0, 1fr))" in components
    assert "width: min(420px, 100%)" in components
    assert "margin: 0" in components
    assert "padding: 0" in components
    assert "background: transparent" in components
    assert "border-bottom: 2px solid transparent" in components
    assert "border-bottom-color: #5d8ad6" in components
    assert "height: calc(100vh - 315px)" in components
    assert "align-items: stretch" in components
    assert ".recommendation-layout.is-long .table-region" in components
    assert "gap: 12px" in components
    assert "padding: 12px" in components
    assert "table.is-long-table" in components
    assert ".long-sidebar" in components
    assert "flex-direction: column" in components
    assert ".long-stock-header" in components
    assert ".long-panel-heading::before" in components
    assert ".long-stock-header::before" in components
    assert ".is-long-table tbody tr:hover" in components
    assert ".long-industry-label" in components
    assert ".long-industry-average" in components
    assert ".long-industry-average.is-unavailable" in components


def test_long_scope_controls_follow_the_long_strategy_description() -> None:
    template = (ROOT / "src/trader/web/templates/index.html").read_text(encoding="utf-8")
    long_groups = (ROOT / "src/trader/web/static/long_groups.js").read_text(encoding="utf-8")
    design = (ROOT / "docs/software-business-design.md").read_text(encoding="utf-8")

    description_end = template.index("</p>", template.index('id="strategyDescription"'))
    scope_start = template.index('id="longScopeTabs"')
    strategy_choice_end = template.index("</div>", scope_start)

    assert description_end < scope_start < strategy_choice_end
    assert 'data-scope="chokepoint">卡脖子行业</button>' in template
    assert 'data-scope="future_growth">高成长赛道</button>' in template
    assert 'data-scope="low_price_potential">低价潜力股</button>' in template
    render = (ROOT / "src/trader/web/static/render.js").read_text(encoding="utf-8")
    assert "long_section_divider" in render
    assert "has-long-section-divider" in render
    assert "els.longScopeTabs.hidden = !isLong" not in long_groups
    assert "groupAveragePct" in long_groups
    assert "long-industry-average" in long_groups
    assert "有效行情" in long_groups
    assert "有效行情股票的当日涨跌幅等权算术平均值" in design
    assert "整组没有有效行情时显示 `--`" in design


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

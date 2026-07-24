from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_dashboard_shows_only_the_primary_recommendation_table() -> None:
    template = (ROOT / "src/trader/web/templates/index.html").read_text(encoding="utf-8")
    dashboard = (ROOT / "src/trader/web/static/dashboard.js").read_text(encoding="utf-8")
    selection = (ROOT / "src/trader/web/static/selection.js").read_text(encoding="utf-8")

    assert 'id="recommendationTable"' in template
    assert 'id="recommendationLayout"' in template
    assert 'id="longGroupBar"' in template
    assert 'id="longScopeTabs"' in template
    assert 'id="longPanelTitle"' in template
    assert 'id="longIndustryTabs"' in template
    assert 'id="longStockHeader"' in template
    description_end = template.index("</p>", template.index('id="strategyDescription"'))
    strategy_choice_end = template.index("</div>", description_end)
    assert description_end < template.index('id="longScopeTabs"') < strategy_choice_end
    assert template.index('id="longScopeTabs"') < template.index('id="recommendationLayout"')
    assert template.index('id="longGroupBar"') < template.index('id="longStockHeader"')
    assert 'data-scope="low_price_potential"' in template
    assert "selection.js', v='3'" in template
    assert 'id="tableTitle"' not in template
    assert 'id="watchTable"' not in template
    assert "观察列表" not in template
    assert "最高评分" in dashboard
    assert "低于观察门槛" in dashboard
    assert "当前没有达到正式推荐条件的股票" in dashboard
    assert "长期策略当前尚无可用数据" in dashboard
    assert "当前暂无可用荐股数据" in dashboard
    assert "当前策略尚未发布快照" not in dashboard
    assert "visibleRecommendations(payload)" in dashboard
    assert 'item.action === "executable"' in selection
    assert 'payload.strategy === "long"' in dashboard
    assert 'setLongControls(nextStrategy === "long")' in dashboard
    assert 'setLongControls(state.strategy === "long")' in dashboard
    assert "els.longScopeTabs.hidden = !enabled" in dashboard
    assert 'new URLSearchParams(strategy === "long" ? {} : { top_n: "18" })' in dashboard
    assert "tableDefinition(payload)" in dashboard
    assert 'payload.phase === "close_fallback"' in selection
    assert 'items.filter((item) => item.action === "observe")' not in dashboard


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
    assert ".long-group-bar" in components
    assert "flex-direction: column" in components
    assert ".long-stock-header" in components


def test_long_scope_controls_follow_the_long_strategy_description() -> None:
    template = (ROOT / "src/trader/web/templates/index.html").read_text(encoding="utf-8")
    long_groups = (ROOT / "src/trader/web/static/long_groups.js").read_text(encoding="utf-8")

    description_end = template.index("</p>", template.index('id="strategyDescription"'))
    scope_start = template.index('id="longScopeTabs"')
    strategy_choice_end = template.index("</div>", scope_start)

    assert description_end < scope_start < strategy_choice_end
    assert 'data-scope="chokepoint">卡脖子行业</button>' in template
    assert 'data-scope="future_growth">高成长赛道</button>' in template
    assert 'data-scope="low_price_potential">低价潜力股</button>' in template
    assert "els.longScopeTabs.hidden = !isLong" not in long_groups


def test_packaged_long_watchlist_matches_runtime_configuration() -> None:
    config = json.loads((ROOT / "config/v2/long_watchlist.json").read_text(encoding="utf-8"))
    source = (ROOT / "src/trader/web/static/long_watchlist_data.js").read_text(encoding="utf-8").strip()
    prefix = '(function(){"use strict";window.TraderLongWatchlistData=Object.freeze('
    suffix = ");})();"

    assert source.startswith(prefix)
    assert source.rstrip().endswith(suffix)
    packaged = json.loads(source[len(prefix) : -len(suffix)])
    assert packaged == config

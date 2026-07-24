from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_dashboard_shows_only_the_primary_recommendation_table() -> None:
    template = (ROOT / "src/trader/web/templates/index.html").read_text(encoding="utf-8")
    dashboard = (ROOT / "src/trader/web/static/dashboard.js").read_text(encoding="utf-8")
    selection = (ROOT / "src/trader/web/static/selection.js").read_text(encoding="utf-8")

    assert 'id="recommendationTable"' in template
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
    assert 'payload.phase === "close_fallback"' in selection
    assert 'items.filter((item) => item.action === "observe")' not in dashboard


def test_web_schema_exposes_additive_downside_projection() -> None:
    schema = (ROOT / "src/trader/web/schemas.py").read_text(encoding="utf-8")

    assert '"setup_type"' in schema
    assert '"downside"' in schema

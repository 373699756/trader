from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_dashboard_keeps_the_fixed_long_watchlist_tabs() -> None:
    template = (ROOT / "src/trader/web/templates/index.html").read_text(encoding="utf-8")
    groups = (ROOT / "src/trader/web/static/long_groups.js").read_text(encoding="utf-8")
    dashboard = (ROOT / "src/trader/web/static/dashboard.js").read_text(encoding="utf-8")

    assert 'id="longScopeTabs"' in template
    assert 'data-scope="chokepoint"' in template
    assert 'data-scope="future_growth"' in template
    assert 'data-scope="low_price_potential"' in template
    assert '"卡脖子行业"' in groups
    assert '"高成长赛道"' in groups
    assert '"低价潜力股"' in groups
    assert "/api/decisions/" in dashboard
    assert "/api/v2/" not in dashboard
    assert "/api/recommendations/" not in dashboard

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


def test_dashboard_labels_high_scores_as_strategy_local() -> None:
    template = (ROOT / "src/trader/web/templates/index.html").read_text(encoding="utf-8")
    scoring = (ROOT / "docs/01_评分逻辑.md").read_text(encoding="utf-8")
    design = (ROOT / "docs/02_工程设计.md").read_text(encoding="utf-8")
    normalized_scoring = " ".join(scoring.split())
    normalized_design = " ".join(design.split())

    assert "策略内评分最高" in template
    assert "仅在当前策略内比较" in template
    assert "页面上的 0–100 分只允许在当前策略内排序和解释，不得直接横向比较" in normalized_scoring
    assert "Decision coverage、 GET、SSE 完整替换和运行诊断统一读取该聚合" in normalized_design

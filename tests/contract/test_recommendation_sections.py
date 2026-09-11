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


def test_dashboard_and_explanation_use_the_unified_short_horizon_score_scale() -> None:
    template = (ROOT / "src/trader/web/templates/index.html").read_text(encoding="utf-8")
    scoring = (ROOT / "docs/01_评分逻辑.md").read_text(encoding="utf-8")
    design = (ROOT / "docs/02_工程设计.md").read_text(encoding="utf-8")
    retrospective = (ROOT / "docs/04_策略回溯.md").read_text(encoding="utf-8")
    normalized_scoring = " ".join(scoring.split())
    normalized_design = " ".join(design.split())
    normalized_retrospective = " ".join(retrospective.split())

    assert "统一评分最高" in template
    assert "今 / 明 / 2–5 日统一 0–100 标尺" in template
    assert "Today、Tomorrow、D25 的最终分共享同一 0–100 质量标尺" in normalized_scoring
    assert "模型相对排名只作为诊断" in normalized_scoring
    assert "Decision coverage、 GET、SSE 完整替换和运行诊断统一读取该聚合" in normalized_design
    assert "预测横截面分位只作为模型相对信号诊断" in normalized_retrospective
    assert "Tomorrow | 14:50 到下一交易日收盘的成本后净超额，14:50 冻结 | 板块证据质量权重" in (
        normalized_retrospective
    )
    assert "作为 Tomorrow `base_score`" not in retrospective

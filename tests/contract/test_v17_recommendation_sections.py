from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_dashboard_has_separate_recommendation_and_watch_sections() -> None:
    template = (ROOT / "src/trader/web/templates/index.html").read_text(encoding="utf-8")
    dashboard = (ROOT / "src/trader/web/static/dashboard.js").read_text(encoding="utf-8")

    assert 'id="recommendationTable"' in template
    assert 'id="watchTable"' in template
    assert "当前无通过下行保护的正式推荐" in dashboard
    assert 'items.filter((item) => item.action === "executable")' in dashboard
    assert 'items.filter((item) => item.action === "observe")' in dashboard


def test_web_schema_exposes_additive_downside_projection() -> None:
    schema = (ROOT / "src/trader/web/schemas.py").read_text(encoding="utf-8")

    assert '"setup_type"' in schema
    assert '"downside"' in schema

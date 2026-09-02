from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_baostock_history_plan_freezes_daily_scope_and_four_owner_boundaries() -> None:
    strategy = (ROOT / "docs" / "recommendation-strategy.md").read_text(encoding="utf-8")
    design = (ROOT / "docs" / "software-business-design.md").read_text(encoding="utf-8")
    section = strategy[strategy.index("#### 15.1.38") : strategy.index("### 15.2")]

    for required in (
        "score_baostock_daily_core_v1",
        "最近 1500 个交易所开市日",
        "2026-08-31",
        "前复权",
        "未复权",
        "production_authority=false",
        "Codex A",
        "Codex B",
        "Codex C",
        "Codex D",
        "11:20",
        "14:50",
        "不得",
    ):
        assert required in section
    assert "score_baostock_daily_core_v1" in design
    assert "research-baostock-history" in design
    assert "计划中" in design


def test_baostock_plan_does_not_treat_recent_ipos_as_missing_1500_day_rows() -> None:
    strategy = (ROOT / "docs" / "recommendation-strategy.md").read_text(encoding="utf-8")
    section = strategy[strategy.index("#### 15.1.38") : strategy.index("### 15.2")]

    assert "上市日" in section
    assert "应有交易日" in section
    assert "新上市股票" in section
    assert "伪造" in section

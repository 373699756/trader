from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_authorities_separate_implemented_realtime_work_from_inactive_strategy_research() -> None:
    design = (PROJECT_ROOT / "docs" / "software-business-design.md").read_text(encoding="utf-8")
    strategy = (PROJECT_ROOT / "docs" / "recommendation-strategy.md").read_text(encoding="utf-8")

    assert "当前交付状态：V2-E0 至 V2-E11 已完成" in design
    assert "已实施实时与降级基线" not in design
    assert "旧执行模式开关" in design
    assert "versioned_dag" not in design
    assert "versioned_dag" not in strategy
    assert "28 只" in design
    assert "待验证收益路线" in strategy
    assert "不改变当前生产策略" in strategy
    assert "尚未实现" in strategy


def test_t1_authority_fixes_real_production_and_browser_budgets() -> None:
    authority = (PROJECT_ROOT / "docs" / "software-business-design.md").read_text(encoding="utf-8")

    assert "5500 行标准化 250ms、两源合并 600ms、统一快照可读 900ms" in authority
    assert "360 行定向 报价提交 100ms" in " ".join(authority.split())
    assert "SSE 到浏览器下一帧 100ms" in authority
    assert "performance_budgets.schema_version=2" in authority
    assert "不得以占位 DataFrame 或纯序列化替代真实" in authority

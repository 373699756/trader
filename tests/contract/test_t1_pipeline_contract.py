from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_realtime_plan_marks_only_t1_complete_and_keeps_strategy_plan_inactive() -> None:
    realtime_plan = (PROJECT_ROOT / "docs" / "times.md").read_text(encoding="utf-8")
    strategy_plan = (PROJECT_ROOT / "docs" / "strage.md").read_text(encoding="utf-8")
    authority = (PROJECT_ROOT / "docs" / "software-business-design.md").read_text(encoding="utf-8")

    assert "T1 已于 2026-07-23 完成，T2-T5 尚未实施" in realtime_plan
    assert "T1 实施记录" in realtime_plan
    assert "不执行 `docs/strage.md` 中的策略计划" in realtime_plan
    assert "不表示活动策略" in strategy_plan
    assert "`docs/strage.md` 只记录尚未获准实施的收益优化批次" in authority
    assert "`docs/times.md` 按 T1-T5 记录实时" in authority


def test_t1_authority_fixes_real_production_and_browser_budgets() -> None:
    authority = (PROJECT_ROOT / "docs" / "software-business-design.md").read_text(encoding="utf-8")

    assert "5500 行标准化 250ms、两源合并 600ms、统一快照可读 900ms" in authority
    assert "360 行\n定向报价提交 100ms" in authority
    assert "SSE 接收到浏览器下一帧绘制 100ms" in authority
    assert "performance_budgets.schema_version=2" in authority
    assert "不得用 DataFrame self-join、排序或 JSON 序列化占位" in authority

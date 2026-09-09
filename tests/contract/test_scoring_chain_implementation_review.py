from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STRATEGY = ROOT / "docs" / "01_评分逻辑.md"
DESIGN = ROOT / "docs" / "02_工程设计.md"
WORK = ROOT / "docs" / "03_工程实施.md"
REVIEW = ROOT / "docs" / "reports" / "2026-09-10-scoring-chain-implementation-review.md"


def test_scoring_chain_review_is_linked_and_separates_implemented_from_remaining_work() -> None:
    strategy = STRATEGY.read_text(encoding="utf-8")
    design = DESIGN.read_text(encoding="utf-8")
    work = WORK.read_text(encoding="utf-8")
    review = REVIEW.read_text(encoding="utf-8")

    assert "两类形态独立判定" in strategy
    assert "selection_rank" in strategy
    assert "selection_rank" in design
    assert "2026-09-10-scoring-chain-implementation-review.md" in work
    assert "## 已实现的生产链" in review
    assert "## 本批发现并修复的代码不一致" in review
    assert "## 尚未实现或尚未闭合" in review
    for marker in (
        "D25 入场分支",
        "集中度补位",
        "最高分同分排序",
        "板块身份展示",
        "ShadowMonitor",
        "终端留出",
        "人工生产授权",
    ):
        assert marker in review


def test_v3_remaining_plan_does_not_repeat_the_resolved_training_cost_mismatch() -> None:
    work = WORK.read_text(encoding="utf-8")

    assert "训练目标已是扣成本前的基准超额" in work
    assert "训练数据合同仍把 20/50/100bp 往返成本写进标签" not in work
    assert "训练样本也仍保存 `net_excess_returns`" not in work

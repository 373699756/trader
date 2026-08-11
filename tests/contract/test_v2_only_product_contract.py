from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_authoritative_design_requires_one_final_v2_product_chain_without_runtime_compatibility() -> None:
    design = (ROOT / "docs/software-business-design.md").read_text(encoding="utf-8")

    for statement in (
        "V2-only 最终 release 边界",
        "当前交付状态：V2-E0 至 V2-E7 已完成，V2-E8 至 V2-E11 尚未交付",
        "新 release 不读取旧运行目录、旧数据库、旧快照或旧 schema",
        "V2 唯一运行目录固定为 `.runtime/v2`",
        "旧 release 只能与其对应旧运行目录整体回退",
        "不得提供旧 API 别名、重定向、弃用窗口、双读或双写",
        "GET /api/v2/decisions/<strategy>/current",
        "GET /api/v2/decisions/<strategy>/history?date=YYYY-MM-DD",
        "GET /api/v2/decisions/<strategy>/dates",
        "GET /api/v2/status",
        "GET /api/v2/events",
    ):
        assert statement in design


def test_strategy_contract_requires_v2_native_decisions_and_no_legacy_replay() -> None:
    strategy = (ROOT / "docs/recommendation-strategy.md").read_text(encoding="utf-8")
    compact = " ".join(strategy.split())

    for statement in (
        "最终 V2 评分口径只产生 `ScoredDecision`",
        "新 release 不得构造或读取旧 `RecommendationSnapshot`",
        "不回放旧策略、旧引擎或旧 schema",
        "long 不借用评分字段伪造荐股决策形状",
    ):
        assert statement in compact


def test_v2_execution_plan_has_no_compatibility_or_shadow_cutover_batch() -> None:
    overview = (ROOT / "docs/V2.md").read_text(encoding="utf-8")
    plan = (ROOT / "docs/implementation-plan.md").read_text(encoding="utf-8")

    assert "状态：V2-only 目标已确认" in overview
    assert "不保留旧版运行时兼容" in overview
    assert "V2-E0、V2-E1、V2-E2、V2-E3、V2-E4、V2-E5、V2-E6、V2-E7" in plan
    assert "下一工程章节为 V2-E8" in plan
    assert "### V2-E0：唯一产品契约重置（已完成）" in plan
    assert "### V2-E10：删除旧生产链" in plan
    assert "### V2-E11：最终验收与发布" in plan
    assert "旧 API 的弃用窗口" not in plan
    assert "并行影子后原子切换" not in plan
    assert "历史兼容解码器" not in plan
    assert not (ROOT / "docs/V2_plan.md").exists()
    assert not (ROOT / "docs/score.md").exists()

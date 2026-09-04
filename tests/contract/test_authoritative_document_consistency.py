from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DESIGN = ROOT / "docs" / "software-business-design.md"
STRATEGY = ROOT / "docs" / "recommendation-strategy.md"


def test_authoritative_docs_record_completed_gates_without_claiming_a_formal_release() -> None:
    design = DESIGN.read_text(encoding="utf-8")
    strategy = STRATEGY.read_text(encoding="utf-8")

    assert "当前交付状态：V2-only 工程与发布门禁验收已闭合" in design
    assert "发布候选契约" in design
    assert "旧链已从活动树物理删除" in strategy
    assert "V2-only 是唯一活动产品链" in design
    assert "正式 0.2.0 release 尚未声明" in strategy
    assert "当前版本仍为 Unreleased" in design


def test_authoritative_docs_do_not_retain_superseded_migration_chronology() -> None:
    design = DESIGN.read_text(encoding="utf-8")
    strategy = STRATEGY.read_text(encoding="utf-8")

    for obsolete in (
        "tomorrow v2 影子运行与切换门禁交付边界",
        "tomorrow v2 原生输入驱动流水线交付边界",
        "tomorrow v2 切换证据持久化与离线复核交付边界",
        "tomorrow v2 影子同批输入收敛交付边界",
        "tomorrow v2 跨日启动与证据窗口隔离交付边界",
        "真实 v27",
        "真实 v28",
        "真实 v29",
        "v17 P1-P6 活动实现",
        "P3-P6 公共接缝",
        "版本身份提升为 v30",
        "正式接管边界",
        "TodayV2Runtime",
        "TomorrowV2Runtime",
        "截至 V2-E7",
        "V2-E9 再把进程级组合根",
        "V2-E8 交付后还要",
    ):
        assert obsolete not in design

    assert "迁移过程、事故复盘和逐批实现 记录只保存在 `CHANGELOG.md`" in _compact(design)
    assert "历史迁移门禁比较 v1/v2" not in strategy
    for obsolete_identity in ("DecisionEpoch", "CurrentDecisionIndex", ".runtime/v17"):
        assert obsolete_identity not in design
        assert obsolete_identity not in strategy


def test_freeze_contract_has_one_boundary_for_each_strategy() -> None:
    design = DESIGN.read_text(encoding="utf-8")
    strategy = STRATEGY.read_text(encoding="utf-8")

    assert "11:20:00 或之后启动" in design
    assert "禁止 checkpoint" in design
    assert "14:49:20（含）至 14:50（不含）" in design
    assert "| 11:19:50 | today 冻结检查点 |" not in design
    assert "有效 14:49:50 检查点" not in design
    assert "`close_fallback` 的 today" not in strategy
    assert "同日也不允许 `close_fallback`" in _compact(strategy)


def test_strategy_uses_current_decision_types_and_risk_aware_upper_bound() -> None:
    strategy = STRATEGY.read_text(encoding="utf-8")

    assert "local `ScoredDecision`" in strategy
    assert "hybrid `ScoredDecision`" in _compact(strategy)
    assert "`UnifiedDecisionIndex`" in strategy
    assert "`LongProjection`" in strategy
    assert "long 永远只发布 local" not in strategy
    assert "local/hybrid DecisionEpoch" not in strategy
    assert "mandatory_known_local_risk_penalty" in strategy
    assert "不得假定已确认风险消失" in strategy
    assert "tomorrow 独占正常目标 36、硬上限 66" not in strategy
    assert "tomorrow 独占目标 21、硬上限 38" in strategy


def test_authoritative_docs_keep_algorithm_and_product_ownership_separate() -> None:
    design = DESIGN.read_text(encoding="utf-8")
    strategy = STRATEGY.read_text(encoding="utf-8")

    assert "mandatory_known_local_risk_penalty" not in design
    assert "mandatory_known_local_risk_penalty" in strategy
    assert "GET /api/decisions/<strategy>/current" in design
    assert "GET /api/decisions/<strategy>/current" not in strategy
    assert "GET /api/v2/decisions/<strategy>/current" not in design
    assert "GET /api/status" in design
    assert "GET /api/status" not in strategy
    assert "DeepSeek 单次网络 timeout 20 秒" in _compact(design)
    assert "单次网络 timeout 为 20 秒" in strategy


def test_authoritative_docs_preserve_strict_qfq_equivalence_boundary() -> None:
    design = DESIGN.read_text(encoding="utf-8")
    strategy = STRATEGY.read_text(encoding="utf-8")
    contract = "逐行公司行动元数据为空且两个调整标志均为零"

    assert contract in design
    assert contract in strategy
    assert "不得把一般未复权 `day` 标记为 qfq" in design
    assert "不得把一般未复权 `day` 标记为 qfq" in strategy


def _compact(content: str) -> str:
    return " ".join(content.split())

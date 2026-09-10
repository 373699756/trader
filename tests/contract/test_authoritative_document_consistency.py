from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DESIGN = ROOT / "docs" / "02_工程设计.md"
STRATEGY = ROOT / "docs" / "01_评分逻辑.md"


def test_authoritative_design_defers_delivery_status_to_the_work_plan() -> None:
    design = DESIGN.read_text(encoding="utf-8")
    strategy = STRATEGY.read_text(encoding="utf-8")
    work = (ROOT / "docs/03_工程实施.md").read_text(encoding="utf-8")

    assert "交付状态只由 `03_工程实施.md` 维护" in design
    assert "本文件只维护尚未完成的工程任务" in work
    assert "`completed`" not in work
    assert "baostock_increment_archive" not in work
    assert "dynamic_cutoff_and_missing_fact_acquisition" not in work
    assert "candidate_single_owner_acceptance_revalidation" not in work
    assert "scoring_weight_configuration_single_source" not in work
    assert "v3_training_artifact_rebuild" in work
    assert "当前交付状态：current-only 工程与发布门禁验收已闭合" not in design
    assert "当前代码仍属于 `Unreleased`" not in design
    assert "正式 0.2.0 release 尚未声明" not in strategy
    assert "旧链已从活动树物理删除" not in strategy


def test_authoritative_docs_do_not_retain_superseded_migration_chronology() -> None:
    design = DESIGN.read_text(encoding="utf-8")
    strategy = STRATEGY.read_text(encoding="utf-8")

    for obsolete in (
        "tomorrow 旧影子运行与切换门禁交付边界",
        "tomorrow 旧原生输入驱动流水线交付边界",
        "tomorrow 旧切换证据持久化与离线复核交付边界",
        "tomorrow 旧影子同批输入收敛交付边界",
        "tomorrow 旧跨日启动与证据窗口隔离交付边界",
        "真实 v27",
        "真实 v28",
        "真实 v29",
        "v17 P1-P6 活动实现",
        "P3-P6 公共接缝",
        "版本身份提升为 v30",
        "正式接管边界",
        "TodayV2Runtime",
        "TomorrowV2Runtime",
        "截至旧迁移 E7",
        "旧迁移 E9 再把进程级组合根",
        "旧迁移 E8 交付后还要",
    ):
        assert obsolete not in design

    assert "历史证据统一见[工程实施](03_工程实施.md)与 `CHANGELOG.md`" in _compact(design)
    assert "历史迁移门禁比较旧档位" not in strategy
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

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DESIGN = ROOT / "docs" / "02_工程设计.md"
STRATEGY = ROOT / "docs" / "01_评分逻辑.md"


def test_freeze_contract_has_one_boundary_for_each_strategy() -> None:
    design = DESIGN.read_text(encoding="utf-8")
    strategy = STRATEGY.read_text(encoding="utf-8")

    assert "14:49:20（含）至 14:50（不含）" in design
    assert "有效 14:49:50 检查点" not in design
    assert "Tomorrow/D25 15:00 后" in strategy
    assert "Long：不冻结、不写推荐历史" in strategy


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

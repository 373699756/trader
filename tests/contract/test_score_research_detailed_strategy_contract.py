from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STRATEGY = ROOT / "docs" / "recommendation-strategy.md"


def test_score_research_strategy_freezes_candidate_recall_and_challenger_formulas() -> None:
    strategy = " ".join(STRATEGY.read_text(encoding="utf-8").split())

    for token in (
        "coverage = sum(known_weight)",
        "coverage_shrunk_score = 50 + coverage * (known_weighted_mean - 50)",
        "optimistic_component_score = sum(known_weight * known_score) + sum(missing_weight * 100)",
        "hybrid_upper_bound = clamp(local_upper_bound * 0.68",
        "active-set",
        "candidate_upper_bound",
        "continuous_entry",
        "coverage_shrink",
        "heat_weak_structure",
        "combined_v1",
        "不超过 30% 及所有既有资格门",
        "未超过板块硬热度上限但落入预注册高热带",
    ):
        assert token in strategy


def test_score_r4_preregisters_entry_and_heat_thresholds_before_replay() -> None:
    strategy = " ".join(STRATEGY.read_text(encoding="utf-8").split())

    for token in (
        "score_r4_preregistered_parameters_v1",
        "score_r4_entry_parameters_v1",
        "score_r4_heat_parameters_v1",
        "`ma5_ma10_spread_pct`",
        "-0.50% | 0.00% | 0.50%",
        "0.60 | 0.70 | 0.80",
        "1.80 | 2.00 | 2.20",
        "65.00 | 70.00 | 75.00",
        "`[6.00%, 8.00%]`",
        "`[12.00%, 16.00%]`",
        "`close_location <= 35.00`",
        "`tail_return_30m_pct <= -0.50%`",
        "`intraday_drawdown_pct >= 3.00%`",
        "R4 只生成配对 manifest，不执行 R5 bootstrap、Holm 或晋级判断",
    ):
        assert token in strategy


def test_score_research_strategy_freezes_pairing_bootstrap_and_promotion_boundary() -> None:
    strategy = " ".join(STRATEGY.read_text(encoding="utf-8").split())

    for token in (
        "同日同股贡献配对",
        "未选中股票的组合权重为 0",
        "非循环连续区块",
        "(extreme_count + 1) / (10000 + 1)",
        "Holm step-down",
        "p_i <= 0.05 / (5 - i + 1)",
        "historical_rejected",
        "forward_collecting",
        "promotion_eligible",
        "2026-11-02",
        "2026-11-27",
        "不得因实现了 collector 就宣称前向证据完成",
        "`no_decision`",
        "才是有效零暴露日",
    ):
        assert token in strategy


def test_score_research_strategy_keeps_production_unchanged_until_manual_release() -> None:
    strategy = " ".join(STRATEGY.read_text(encoding="utf-8").split())

    assert "weight(lambda) = (1 - lambda) * current_weight + lambda * candidate_weight" in strategy
    assert "Score-R6 不得 复用 Score-R0 的 40+20 评价窗口" in strategy
    assert "PromotionDossier" in strategy
    assert "研究状态变化不得直接写活动策略配置" in strategy
    assert "DeepSeek 物理 HTTP 请求增量必须为 0" in strategy

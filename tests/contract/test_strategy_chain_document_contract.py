from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STRATEGY = ROOT / "docs/01_评分逻辑.md"


def test_strategy_document_follows_the_end_to_end_stock_selection_chain() -> None:
    content = STRATEGY.read_text(encoding="utf-8")
    ordered_sections = (
        "## 1. 目标、范围与当前结论",
        "## 2. 端到端荐股链路",
        "## 3. 数据采集、点时归一化与数据就绪",
        "## 4. 两级硬过滤",
        "## 5. 板内总体、候选预选与评分资格",
        "## 6. 本地与模型评分",
        "## 7. 本地风险与 DeepSeek 结构化复核",
        "## 8. 融合、动作、稳定排名与 TopK",
        "## 9. 冻结、恢复与不可变历史",
        "## 10. 展示与可解释性",
        "## 11. 逐环节优化空间与优先级",
        "## 12. 收益验证与变更门禁",
        "## 13. 链路外边界",
    )

    positions = tuple(content.index(section) for section in ordered_sections)
    assert positions == tuple(sorted(positions))
    assert len(content.splitlines()) <= 900


def test_every_chain_stage_has_realtime_stability_and_return_review() -> None:
    content = STRATEGY.read_text(encoding="utf-8")
    review = content[content.index("## 11. 逐环节优化空间与优先级") :]

    for column in ("实时性", "稳定性", "提高荐股收益"):
        assert column in review
    for stage in (
        "数据采集与合并",
        "一级资格过滤",
        "二级动态过滤",
        "板内总体",
        "候选预选",
        "评分资格",
        "本地/模型评分",
        "本地风险",
        "DeepSeek 复核",
        "融合与动作",
        "TopK 与集中度",
        "冻结与恢复",
        "展示与反馈",
    ):
        assert stage in review
    assert "所有优化项当前均为研究建议" in review
    assert "不能宣称提高收益" in review


def test_strategy_document_removes_delivery_history_and_implementation_noise() -> None:
    content = STRATEGY.read_text(encoding="utf-8")

    for removed in (
        "engine_review28_2026_07",
        "#### 15.1.9",
        "#### 15.1.10",
        "#### 15.1.11",
        "#### 15.1.12",
        "#### 15.1.13",
        "#### 15.1.14",
        "#### 15.1.16",
        "#### 15.1.17",
        "#### 15.1.18",
        "#### 15.1.19",
        "#### 15.1.20",
        "Codex A",
        "Codex B",
        "Codex C",
        "Codex D",
        "CHOKEPOINT_INDUSTRY_LEADERS",
        "docs/reports/a-share-long-industry-research",
    ):
        assert removed not in content


def test_fixed_scoring_and_freeze_invariants_remain_explicit() -> None:
    content = STRATEGY.read_text(encoding="utf-8")

    for invariant in (
        "candidate_score >= 50",
        "board_data_reliability >= 0.85",
        "local_score = clamp(base_score - local_risk_penalty, 0, 100)",
        "local_score * 0.68",
        "+ deepseek_score * 0.32",
        "- deepseek_risk_penalty",
        "ROUND_HALF_UP",
        "83.40",
        "today 11:20",
        "tomorrow/d25 14:50",
        "单一板块最多 `ceil(top_n * 60%)`",
        "同一行业最多 2 只",
    ):
        assert invariant in content


def test_d25_means_one_future_t2_to_t5_rising_stock_signal() -> None:
    content = STRATEGY.read_text(encoding="utf-8")

    for required in (
        "未来第 2 至第 5 个交易日区间内具备上涨能力的股票",
        "T+2、T+3、T+4、T+5",
        "一个完整且唯一的 2–5 日生产策略头",
        "当前 D25 使用规则评分，不读取 Tomorrow 训练模型",
    ):
        assert required in content

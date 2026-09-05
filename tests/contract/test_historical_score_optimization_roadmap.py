from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _compact(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_strategy_roadmap_is_ordered_benefit_first_and_strategy_complete() -> None:
    work = _compact(ROOT / "docs/work.md")

    ordered_sections = (
        "## 1. 当前基线",
        "## 2. 评分模块化交付记录",
        "## 3. 历史评分研究路线",
        "### 3.1 已封存章节",
        "### 3.2 依赖状态",
        "### 3.3 V3 训练与验证（15.1.35–15.1.36）",
        "### 3.4 BaoStock 2000 日归档（15.1.38）",
        "## 4. 交付与验证",
    )
    positions = tuple(work.index(section) for section in ordered_sections)
    assert positions == tuple(sorted(positions))
    assert "已封存" in work

    for token in (
        "最终留出至少 200 个交易日",
        "Today 11:20",
        "Tomorrow 14:50",
        "D25 14:50",
        "Holm",
        "download_history",
        "train-tomorrow",
        "production_authority=false",
    ):
        assert token in work


def test_roadmap_cannot_restore_forward_collection_or_reuse_observed_holdout_as_blind() -> None:
    strategy = _compact(ROOT / "docs/recommendation-strategy.md")
    work = _compact(ROOT / "docs/work.md")
    design = _compact(ROOT / "docs/software-business-design.md")

    for token in (
        "不得恢复未来日 collector",
        "不得把既有 139 日窗口重新命名为独立盲测",
        "线上 outcome 仍只用于正式推荐历史、运行监控和回退告警",
        "每次“继续”只交付下一个完整未完成章节",
        "不得定时、在线或无人授权地自动训练/调参、自动晋级、自动激活或自动回退",
        "用户显式调用第 15.1.20 节的 `train-tomorrow`",
    ):
        assert token in work
    assert "开发工作计划" in design
    assert "15.1.21–15.1.34" in work

    for removed_section in (
        "15.1.28 历史 DeepSeek 点时事实与增量证据",
        "15.1.29 自适应收益、成本与风险模型族",
        "15.1.31 组合净效用与约束选择挑战者",
        "15.1.36 严重亏损概率与市场状态稳健性",
        "15.1.37 评分热链确定性与资源效率门禁",
        "15.1.38 自动训练、漂移与挑战者生成",
        "15.1.39 受控自动晋级、启动激活与回退",
        "15.1.40 跨策略结论、生产接入与最终授权",
    ):
        assert removed_section not in strategy


def test_each_strategy_gate_requires_point_in_time_parity_and_terminal_evidence() -> None:
    strategy = _compact(ROOT / "docs/recommendation-strategy.md")

    for token in (
        "historical_point_in_time_parity",
        "historical_data_insufficient",
        "terminal_holdout_not_opened",
        "合法空仓日必须结算",
        "20bp 与 50bp",
        "严重亏损率不得高于同口径生产基准",
        "固定 68/32 融合",
        "DeepSeek 自由文本",
        "automatic_model_update=false",
        "最终留出至少 200 个交易日",
    ):
        assert token in strategy

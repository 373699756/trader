from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _compact(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_strategy_roadmap_is_ordered_historical_only_and_strategy_complete() -> None:
    strategy = _compact(ROOT / "docs/recommendation-strategy.md")

    ordered_sections = (
        "15.1.21 历史评分优化总序与批次纪律",
        "15.1.22 H1 扩展历史归档与点时覆盖审计",
        "15.1.23 三策略标签、基准与切分预注册",
        "15.1.24 嵌套时序选择与多重检验控制",
        "15.1.25 Today 端到端历史留出",
        "15.1.26 Tomorrow 新挑战者历史留出",
        "15.1.27 D25 端到端历史留出",
        "15.1.28 严重亏损概率与市场状态稳健性",
        "15.1.29 跨策略结论与生产授权边界",
    )
    positions = tuple(strategy.index(section) for section in ordered_sections)
    assert positions == tuple(sorted(positions))

    for token in (
        "每只股票最多 1600 个历史交易日",
        "共同有效交易日少于 1000 日",
        "最旧 60%、随后 20%、最新 20%",
        "最终留出至少 200 个交易日",
        "Today 11:20",
        "Tomorrow 14:50",
        "D25 14:50",
        "Holm",
        "日期分组的移动区块 bootstrap",
        "research-screen` 顺序执行六个历史阶段",
        "production_authority=false",
    ):
        assert token in strategy


def test_roadmap_cannot_restore_forward_collection_or_reuse_observed_holdout_as_blind() -> None:
    strategy = _compact(ROOT / "docs/recommendation-strategy.md")
    design = _compact(ROOT / "docs/software-business-design.md")

    for token in (
        "不得恢复未来日 collector",
        "不得把既有 139 日窗口重新命名为独立盲测",
        "线上 outcome 仍只用于正式推荐历史和运行监控",
        "每次“继续”只交付下一个完整未完成章节",
    ):
        assert token in strategy
    assert "历史评分优化路线的执行顺序、样本门槛和研究终态" in design


def test_each_strategy_gate_requires_point_in_time_parity_and_terminal_evidence() -> None:
    strategy = _compact(ROOT / "docs/recommendation-strategy.md")

    for token in (
        "historical_point_in_time_parity",
        "historical_data_insufficient",
        "terminal_holdout_not_opened",
        "合法空仓日必须结算",
        "20bp 与 50bp",
        "严重亏损率不得高于同口径生产基准",
        "历史通过不能自动改配置、权重、阈值、模型或生产档位",
    ):
        assert token in strategy

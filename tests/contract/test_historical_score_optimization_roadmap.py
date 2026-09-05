from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _compact(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_strategy_roadmap_is_ordered_benefit_first_and_strategy_complete() -> None:
    strategy = _compact(ROOT / "docs/recommendation-strategy.md")

    ordered_sections = (
        "15.1.21 历史评分验证总序与批次纪律",
        "15.1.22 一级永久资格名单与二级硬过滤",
        "15.1.23 现有基线身份与结论一致性审计",
        "15.1.24 评分热链基线与等价效率门禁",
        "15.1.25 H1 分策略历史归档与点时覆盖审计",
        "15.1.26 三策略标签、基准与切分预注册",
        "15.1.27 全候选预测—实际残差账本",
        "15.1.28 过滤瀑布与候选召回消融",
        "15.1.29 透明有限候选与成本风险诊断",
        "15.1.30 时序确认与多重检验控制",
        "15.1.31 Today 端到端历史留出",
        "15.1.32 Tomorrow 端到端历史留出",
        "15.1.33 D25 端到端历史留出",
        "15.1.34 跨策略结论与人工变更边界",
    )
    positions = tuple(strategy.index(section) for section in ordered_sections)
    assert positions == tuple(sorted(positions))
    assert "状态：已完成" in strategy[positions[0] : positions[1]]
    assert "状态：已完成" in strategy[positions[1] : positions[2]]
    assert "状态：已完成" in strategy[positions[2] : positions[3]]

    for token in (
        "每只股票最多 1600 个历史交易日",
        "参数化只读能力探针",
        "共同有效交易日少于 1000 日",
        "最旧 60%、随后 20%、最新 20%",
        "最终留出至少 200 个交易日",
        "Today 11:20",
        "Tomorrow 14:50",
        "D25 14:50",
        "Holm",
        "日期分组的移动区块 bootstrap",
        "prediction_error = actual_net_excess_return - predicted_net_excess_return",
        "baseline_identity_consistent",
        "profitable_executable_recall",
        "historical_filter_recall_ablation_report",
        "每策略最多 8 个",
        "每个完成评分 epoch",
        "相同输入的候选、分数、风险、动作、排名和决策 hash 完全一致",
        "参数不得由优化器",
        "DeepSeek 历史盈利增量不在本路线验证",
        "用户另立高风险生产变更批次",
        "旧 H0 历史归档、固定回测和六阶段筛选入口已退役",
        "download_history",
        "train-tomorrow",
        "production_authority=false",
    ):
        assert token in strategy


def test_roadmap_cannot_restore_forward_collection_or_reuse_observed_holdout_as_blind() -> None:
    strategy = _compact(ROOT / "docs/recommendation-strategy.md")
    design = _compact(ROOT / "docs/software-business-design.md")

    for token in (
        "不得恢复未来日 collector",
        "不得把既有 139 日窗口重新命名为独立盲测",
        "线上 outcome 仍只用于正式推荐历史、运行监控和回退告警",
        "每次“继续”只交付下一个完整未完成章节",
        "不得定时、在线或无人授权地自动训练/调参、自动晋级、自动激活或自动回退",
        "用户显式调用第 15.1.20 节的 `train-tomorrow`",
    ):
        assert token in strategy
    assert "历史评分优化路线的执行顺序、样本门槛和研究终态" in design
    assert "第 15.1.21–15.1.34 节" in design

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

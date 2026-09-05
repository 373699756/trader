from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_agent_policy_prefers_architecture_optimal_repairs() -> None:
    policy = (ROOT / "AGENTS.md").read_text(encoding="utf-8")

    assert "### 4.2 工程方案选择与重构原则" in policy
    assert "不得以最小 diff、最少文件、最少改动行数或最短运行链为目标" in policy
    assert "允许纳入有明确整体收益的跨模块或全仓工程重构" in policy
    assert "禁止用“先跑起来”代替完整修复" in policy
    assert "风险匹配的充分验证集" in policy
    assert "只修改当前计划项需要的边界" not in policy
    assert "不得顺带处理相邻章节或无关优化" not in policy
    assert "最小充分验证集" not in policy
    assert "运行与风险相称的最小测试或检查" not in policy


def test_agent_policy_uses_risk_based_quality_gates() -> None:
    policy = (ROOT / "AGENTS.md").read_text(encoding="utf-8")

    assert "#### 4.1.1 大任务与子任务的验证边界" in policy
    assert "子任务完成时只运行与该切片直接相关的定向测试" in policy
    assert "不得因为子任务属于高风险大任务，就在每个子任务末尾重复执行全量测试" in policy
    assert "统一运行一次完整门禁" in policy
    assert "全量结果必须覆盖大任务的完整合并 diff" in policy
    assert "### 5.2 低风险：文档、注释和非运行元数据" in policy
    assert "默认不运行全量 `make test`" in policy
    assert "### 5.3 中风险：局部实现" in policy
    assert "默认不因任意一处" in policy
    assert "### 5.4 高风险与发布门禁" in policy
    assert "最终 release/cutover 验收" in policy
    assert "不要求\n每个高风险子任务重复执行" in policy
    assert "全量门禁待大任务收尾统一执行" in policy
    assert "运行完整质量、测试、构建和仓库外 wheel 安装验收" not in policy

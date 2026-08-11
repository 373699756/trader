from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_agent_policy_uses_risk_based_quality_gates() -> None:
    policy = (ROOT / "AGENTS.md").read_text(encoding="utf-8")

    assert "### 5.2 低风险：文档、注释和非运行元数据" in policy
    assert "默认不运行全量 `make test`" in policy
    assert "### 5.3 中风险：局部实现" in policy
    assert "默认不因任意一处" in policy
    assert "### 5.4 高风险与发布门禁" in policy
    assert "最终 release/cutover 验收" in policy
    assert "未运行的全量门禁应注明“不适用”及理由" in policy
    assert "运行完整质量、测试、构建和仓库外 wheel 安装验收" not in policy

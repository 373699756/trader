from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_continue_command_advances_one_complete_unfinished_section() -> None:
    agents = _section(PROJECT_ROOT / "AGENTS.md", "### 4.1", "### 4.2")
    design = _section(PROJECT_ROOT / "docs/software-business-design.md", "### 15.1", "### 15.2")

    for contract in (agents, design):
        assert "下一个完整未完成章节" in contract
        assert "章节内全部明确子项" in contract
        assert "相邻章节" in contract
        assert "下一个未完成的最小可独立验收任务" not in contract


def test_each_delivery_documents_user_problem_and_change_summary() -> None:
    agents = _section(PROJECT_ROOT / "AGENTS.md", "### 4.5", "## 5")
    design = _section(PROJECT_ROOT / "docs/software-business-design.md", "### 15.1", "### 15.2")

    for contract in (agents, design):
        assert "用户提出的问题" in contract
        assert "修改说明" in contract
        assert "验证证据" in contract
        assert "剩余风险" in contract
        assert "CHANGELOG.md" in contract


def test_docs_keep_two_authorities_plans_and_delivery_reports() -> None:
    docs_root = PROJECT_ROOT / "docs"
    documents = sorted(path.relative_to(docs_root).as_posix() for path in docs_root.rglob("*") if path.is_file())

    assert documents == [
        "plan.md",
        "plan_c.md",
        "plan_sudu.md",
        "plan_youhua.md",
        "recommendation-strategy.md",
        "reports/youhua-a1-baseline.md",
        "reports/youhua-a2-public-skeleton.md",
        "reports/youhua-d1-p6-web.md",
        "reports/youhua-g1-contract-base.md",
        "reports/youhua-g2-gate-review.md",
        "software-business-design.md",
    ]

    design = (docs_root / "software-business-design.md").read_text(encoding="utf-8")
    plan = (docs_root / "plan.md").read_text(encoding="utf-8")
    plan_c = (docs_root / "plan_c.md").read_text(encoding="utf-8")
    plan_sudu = (docs_root / "plan_sudu.md").read_text(encoding="utf-8")
    plan_youhua = (docs_root / "plan_youhua.md").read_text(encoding="utf-8")
    report = (docs_root / "reports/youhua-a1-baseline.md").read_text(encoding="utf-8")
    strategy = (docs_root / "recommendation-strategy.md").read_text(encoding="utf-8")
    assert "软件业务设计文档" in design
    assert "荐股策略文档" in strategy
    assert "非权威执行计划" in plan
    assert "software-business-design.md" in plan
    assert "recommendation-strategy.md" in plan
    assert "非权威执行计划" in plan_c
    assert "software-business-design.md" in plan_c
    assert "recommendation-strategy.md" in plan_c
    assert "非权威执行计划" in plan_sudu
    assert "software-business-design.md" in plan_sudu
    assert "recommendation-strategy.md" in plan_sudu
    assert "非权威执行计划" in plan_youhua
    assert "software-business-design.md" in plan_youhua
    assert "recommendation-strategy.md" in plan_youhua
    assert "A1.x 已完成本地基线采集与契约冻结" in report
    assert "G1 发布" in report
    assert "A2 public skeleton is available" in (docs_root / "reports/youhua-a2-public-skeleton.md").read_text(
        encoding="utf-8"
    )
    assert "G2 已发布" in (docs_root / "reports/youhua-g2-gate-review.md").read_text(encoding="utf-8")
    assert "docs/need.md" not in design
    assert "docs/hi.md" not in design


def _section(path: Path, start: str, end: str) -> str:
    content = path.read_text(encoding="utf-8")
    return content.split(start, 1)[1].split(end, 1)[0]

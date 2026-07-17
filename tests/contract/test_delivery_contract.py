from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_continue_command_advances_one_complete_unfinished_section() -> None:
    agents = _section(PROJECT_ROOT / "AGENTS.md", "### 4.1", "### 4.2")
    need = _section(PROJECT_ROOT / "docs/need.md", "### 24.0", "### 24.1")

    for contract in (agents, need):
        assert "下一个完整未完成章节" in contract
        assert "章节内全部明确子项" in contract
        assert "相邻章节" in contract
        assert "下一个未完成的最小可独立验收任务" not in contract


def test_each_delivery_documents_user_problem_and_change_summary() -> None:
    agents = _section(PROJECT_ROOT / "AGENTS.md", "### 4.5", "## 5")
    need = _section(PROJECT_ROOT / "docs/need.md", "### 24.0", "### 24.1")

    for contract in (agents, need):
        assert "用户提出的问题" in contract
        assert "修改说明" in contract
        assert "验证证据" in contract
        assert "剩余风险" in contract
        assert "CHANGELOG.md" in contract


def _section(path: Path, start: str, end: str) -> str:
    content = path.read_text(encoding="utf-8")
    return content.split(start, 1)[1].split(end, 1)[0]

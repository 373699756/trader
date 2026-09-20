from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BLUEPRINT = ROOT / "docs" / "项目重构详细.md"


def _implementation_section() -> str:
    content = BLUEPRINT.read_text(encoding="utf-8")
    start = content.index("## 10. 强制实施与切换顺序")
    end = content.index("## 11. 结论", start)
    return content[start:end]


def test_refactor_blueprint_has_an_ordered_current_code_migration_plan() -> None:
    section = _implementation_section()

    for phase in range(13):
        assert f"#### 阶段 {phase}：" in section
    positions = tuple(section.index(f"#### 阶段 {phase}：") for phase in range(13))
    assert positions == tuple(sorted(positions))

    for required in (
        "branch/Afuture",
        "feature/tomorrow-v2",
        "当前分支及其上游关系",
        "提交已到达同名上游",
        "Python 版本",
        "工程实施台账",
        "application/history/",
        "application/training/",
        "application/research/",
        "application/recommendation/",
        "infra/scoring/profiles/v2|v3/",
        "http_api/",
        "bootstrap_clock.py",
        "docs/01_评分逻辑.md",
        "docs/02_工程设计.md",
        "tests/contract/test_architecture.py",
        "tests/contract/test_professional_naming_contract.py",
        "Store`、`Repository`、`Archive`、`Registry",
        "data/history/baostock/",
        "data/history/control.sqlite3",
    ):
        assert required in section
    for obsolete in ("<approved-plan-commit>", "HEAD == @{upstream}", "上游 hash", "获批 hash"):
        assert obsolete not in section


def test_refactor_blueprint_plan_closes_release_and_rollback_gates() -> None:
    section = _implementation_section()

    for command in (
        "make format-check",
        "make lint",
        "make type-check",
        "make test",
        "make package",
    ):
        assert command in section
    for required in (
        "83.40",
        "1280x720",
        "1440x900",
        "1920x1080",
        "trader-cli",
        "trader-server",
        "旧 release",
        "不双读、不双写",
        "不得自动合并",
        "仓库外隔离副本",
        "不得触碰活动数据",
    ):
        assert required in section

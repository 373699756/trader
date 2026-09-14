from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APPLICATION = ROOT / "src" / "trader" / "application"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    values: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            values.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            values.add(node.module)
    return values


def test_history_application_package_exposes_split_use_cases_and_ports() -> None:
    package = APPLICATION / "history"
    assert {
        "download_history.py",
        "update_history.py",
        "history_status.py",
        "history_ports.py",
    } <= {path.name for path in package.glob("*.py")}
    assert not any(
        imported.startswith(("trader.infra", "trader.web", "trader.entrypoints"))
        for path in package.glob("*.py")
        for imported in _imports(path)
    )


def test_training_application_package_is_profile_owned_and_infrastructure_free() -> None:
    package = APPLICATION / "training"
    assert {
        "profile_training_primary.py",
        "profile_training_secondary.py",
        "training_due.py",
        "training_ports.py",
    } <= {path.name for path in package.glob("*.py")}
    violations = [
        f"{path.name}: {imported}"
        for path in package.glob("*.py")
        for imported in _imports(path)
        if imported.startswith(("trader.infra", "trader.web", "trader.entrypoints"))
    ]
    assert violations == []


def test_entrypoints_route_download_and_training_through_application_use_cases() -> None:
    cli = (ROOT / "src" / "trader" / "entrypoints" / "cli.py").read_text(encoding="utf-8")
    commands = (ROOT / "src" / "trader" / "entrypoints" / "research_commands.py").read_text(encoding="utf-8")
    assert "DownloadHistoryUseCase" in cli
    assert "TrainV2UseCase" in commands
    assert "TrainV3UseCase" in commands

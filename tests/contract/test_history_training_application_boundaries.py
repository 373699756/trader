from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOWNLOAD_APPLICATION = ROOT / "src" / "trader" / "download" / "application"
TRAINING_APPLICATION = ROOT / "src" / "trader" / "training" / "application"
_SHARED_CAPABILITIES = {
    "trader.infra.workers": {"WorkerExecutor", "ManagedWorkerExecutor", "submit_or_reject"},
    "trader.infra.shutdown": {"ShutdownDeadline", "ShutdownStep"},
}


def application_import_violations(source: str, business: str) -> list[str]:
    tree = ast.parse(source)
    violations: list[str] = []
    forbidden = (
        "trader.infra",
        "trader.download.infra",
        "trader.training.infra",
        "trader.recommendation.infra",
        "trader.http_api",
        "trader.web",
        "trader.entrypoints",
        "trader.download.entrypoints",
        "trader.training.entrypoints",
        "trader.recommendation.entrypoints",
        "flask",
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            violations.extend(alias.name for alias in node.names if alias.name.startswith(forbidden))
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.level and "infra" in node.module.split("."):
                violations.append(node.module)
            elif node.module.startswith(forbidden):
                allowed = _SHARED_CAPABILITIES.get(node.module, set())
                violations.extend(f"{node.module}.{alias.name}" for alias in node.names if alias.name not in allowed)
            else:
                violations.extend(
                    f"{node.module}.{alias.name}"
                    for alias in node.names
                    if f"{node.module}.{alias.name}".startswith(forbidden)
                )
        elif isinstance(node, ast.ImportFrom) and node.level:
            violations.extend(alias.name for alias in node.names if node.module is None and alias.name == "infra")
    return violations


@pytest.mark.parametrize("business", ("download", "training"))
@pytest.mark.parametrize(
    ("source", "rejected"),
    (
        ("from trader.infra.workers import WorkerExecutor, ManagedWorkerExecutor, submit_or_reject", False),
        ("from trader.infra.shutdown import ShutdownDeadline, ShutdownStep", False),
        ("from trader.infra.workers import BoundedExecutor", True),
        ("from trader.infra.workers import *", True),
        ("import trader.infra.workers as workers", True),
        ("from trader.infra import workers", True),
        ("from trader.infra.shutdown import ShutdownSignalController", True),
        ("from trader.BUSINESS.infra import adapter", True),
        ("from trader.BUSINESS import infra", True),
        ("from trader.recommendation.infra import adapter", True),
        ("from ..infra import adapter", True),
        ("from .. import infra", True),
        ("from trader.BUSINESS.entrypoints import commands", True),
        ("from trader.training.entrypoints import commands", True),
        ("from trader.download.entrypoints import commands", True),
        ("from trader.http_api import app", True),
        ("from flask import Flask", True),
        ("import flask", True),
    ),
)
def test_application_guard_accepts_only_narrow_shared_capabilities(business, source, rejected) -> None:
    assert bool(application_import_violations(source.replace("BUSINESS", business), business)) is rejected


def test_history_application_package_exposes_split_use_cases_and_ports() -> None:
    package = DOWNLOAD_APPLICATION
    assert {
        "download_history.py",
        "update_history.py",
        "history_status.py",
        "history_ports.py",
    } <= {path.name for path in package.glob("*.py")}
    violations = [
        f"{path.relative_to(package)}: {imported}"
        for path in package.rglob("*.py")
        for imported in application_import_violations(path.read_text(encoding="utf-8"), "download")
    ]
    assert violations == []


def test_training_application_package_is_profile_owned_and_infrastructure_free() -> None:
    package = TRAINING_APPLICATION
    assert {
        "profile_training_primary.py",
        "profile_training_secondary.py",
        "training_due.py",
        "training_ports.py",
    } <= {path.name for path in package.glob("*.py")}
    violations = [
        f"{path.relative_to(package)}: {imported}"
        for path in package.rglob("*.py")
        for imported in application_import_violations(path.read_text(encoding="utf-8"), "training")
    ]
    assert violations == []


def test_entrypoints_route_download_and_training_through_application_use_cases() -> None:
    commands = (ROOT / "src" / "trader" / "training" / "entrypoints" / "commands.py").read_text(encoding="utf-8")
    download_commands = (ROOT / "src" / "trader" / "download" / "entrypoints" / "commands.py").read_text(
        encoding="utf-8"
    )
    assert "execute_history_download" in download_commands
    assert "DownloadHistoryUseCase" in (ROOT / "src/trader/bootstrap.py").read_text(encoding="utf-8")
    assert "TrainV2UseCase" in commands
    assert "TrainV3UseCase" in commands

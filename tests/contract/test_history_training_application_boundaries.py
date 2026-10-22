from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers.module_boundaries import LayerImportViolations, ParseSourceImports, SourceImports, SourceModuleName

ROOT = Path(__file__).resolve().parents[2]
DOWNLOAD_APPLICATION = ROOT / "src" / "trader" / "download" / "application"
TRAINING_APPLICATION = ROOT / "src" / "trader" / "training" / "application"


@pytest.mark.parametrize("business", ("download", "training"))
@pytest.mark.parametrize(
    ("source", "rejected"),
    (
        ("from trader.infra.workers import WorkerExecutor, ManagedWorkerExecutor, SubmitOrReject", False),
        ("from trader.infra.workers import SubmitWithBackpressure, WorkerResourceRejectedError", False),
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
    module = f"trader.{business}.application.example"
    assert (
        bool(LayerImportViolations(module, ParseSourceImports(source.replace("BUSINESS", business), module)))
        is rejected
    )


def test_history_application_package_exposes_split_use_cases_and_ports() -> None:
    package = DOWNLOAD_APPLICATION
    assert {
        "download_history.py",
        "history_ports.py",
    } <= {path.name for path in package.glob("*.py")}
    violations = [
        f"{path.relative_to(package)}: {imported}"
        for path in package.rglob("*.py")
        for imported in LayerImportViolations(
            SourceModuleName(path, ROOT / "src/trader"),
            SourceImports(path, ROOT / "src/trader"),
        )
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
        for imported in LayerImportViolations(
            SourceModuleName(path, ROOT / "src/trader"),
            SourceImports(path, ROOT / "src/trader"),
        )
    ]
    assert violations == []


def test_entrypoints_route_download_and_joint_training_through_current_owners() -> None:
    commands = (ROOT / "src" / "trader" / "training" / "entrypoints" / "commands.py").read_text(encoding="utf-8")
    download_commands = (ROOT / "src" / "trader" / "download" / "entrypoints" / "commands.py").read_text(
        encoding="utf-8"
    )
    assert "ExecuteHistoryDownload" in download_commands
    assert "DownloadHistoryUseCase" in (ROOT / "src/trader/bootstrap.py").read_text(encoding="utf-8")
    assert "RunJointProfileTraining" in commands
    assert "reuse_matching=True" in commands
    assert "TrainV2UseCase" not in commands
    assert "TrainV3UseCase" not in commands

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from shutil import copy2

import pytest
import tomllib


@pytest.mark.slow
def test_install_keeps_setuptools_metadata_out_of_source_tree(tmp_path: Path) -> None:
    repository = Path(__file__).parents[2]
    isolated_repository = tmp_path / "repository"

    assert 'Path(".build-metadata").mkdir(exist_ok=True)' in (repository / "setup.py").read_text(encoding="utf-8")
    # Reproduce a clean checkout, not caches, previous builds or unrelated local data.
    tracked = (
        subprocess.run(
            [
                "git",
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
                "src",
                "pyproject.toml",
                "setup.py",
                "setup.cfg",
                "README.md",
                "LICENSE*",
            ],
            cwd=repository,
            check=True,
            capture_output=True,
        )
        .stdout.decode("utf-8")
        .split("\0")
    )
    for relative in filter(None, tracked):
        if not (repository / relative).is_file():
            continue
        target = isolated_repository / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        copy2(repository / relative, target)
    hidden_metadata_root = isolated_repository / ".build-metadata"
    source_metadata = isolated_repository / "src" / "trader_research_dashboard.egg-info"
    install_target = isolated_repository / "install-target"

    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--no-index",
            "--disable-pip-version-check",
            "--no-build-isolation",
            "--target",
            str(install_target),
            ".",
        ],
        cwd=isolated_repository,
        check=True,
        capture_output=True,
        text=True,
        timeout=90,
    )

    assert hidden_metadata_root.is_dir()
    assert not source_metadata.exists()


def test_numpy_dependency_upper_bound_keeps_supported_mypy_stub_syntax() -> None:
    repository = Path(__file__).parents[2]
    project = tomllib.loads((repository / "pyproject.toml").read_text(encoding="utf-8"))

    assert "numpy>=2,<2.5" in project["project"]["dependencies"]


def test_http_api_type_resource_and_business_entrypoints_are_packaged() -> None:
    repository = Path(__file__).parents[2]
    project = tomllib.loads((repository / "pyproject.toml").read_text(encoding="utf-8"))
    package_data = project["tool"]["setuptools"]["package-data"]

    assert package_data["trader.http_api"] == ["py.typed"]
    assert (repository / "src/trader/http_api/py.typed").is_file()
    assert (repository / "src/trader/download/entrypoints/history_maintenance_projection.py").is_file()
    assert (repository / "src/trader/training/entrypoints/tomorrow_training_progress.py").is_file()
    assert not (repository / "src/trader/entrypoints/history_automation_projection.py").exists()
    assert not (repository / "src/trader/entrypoints/tomorrow_training_progress.py").exists()
    assert not (repository / "src/trader/entrypoints/h1_point_in_time.py").exists()


def test_hidden_metadata_container_is_not_discovered_as_an_empty_distribution() -> None:
    repository = Path(__file__).parents[2]
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import importlib.metadata as m; "
            "raise SystemExit(any(d.metadata.get('Name') is None for d in m.distributions()))",
        ],
        cwd=repository,
        check=False,
    )

    assert probe.returncode == 0


def test_repository_whitelists_only_portable_model_heads() -> None:
    repository = Path(__file__).parents[2]
    cases = {
        "data/training/models/v2/tomorrow/model.json": False,
        "data/training/models/v2/d25/metadata.json": False,
        "data/training/models/v3/tomorrow/model.json": False,
        "data/training/models/v3/d25/metadata.json": False,
        "data/training/models/v3/d25/active-bundle.json": True,
        "data/training/models/v2/active-profile.json": True,
        "data/training/samples/v2/complete.json": True,
        "data/recommendation/v3/freeze.sqlite3": True,
        "data/market/history/catalog.sqlite3": True,
    }
    for relative, ignored in cases.items():
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "-q", relative],
            cwd=repository,
            check=False,
        )
        assert result.returncode == int(not ignored), relative

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from shutil import copytree

import tomllib


def test_install_keeps_setuptools_metadata_out_of_source_tree(tmp_path: Path) -> None:
    repository = Path(__file__).parents[2]
    isolated_repository = tmp_path / "repository"

    assert 'Path(".build-metadata").mkdir(exist_ok=True)' in (repository / "setup.py").read_text(encoding="utf-8")
    copytree(
        repository,
        isolated_repository,
        ignore=lambda _directory, names: {
            name for name in names if name in {".git", ".venv", ".runtime"} or name.endswith(".egg-info")
        },
    )
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
            "--no-build-isolation",
            "--target",
            str(install_target),
            ".",
        ],
        cwd=isolated_repository,
        check=True,
        capture_output=True,
        text=True,
    )

    assert hidden_metadata_root.is_dir()
    assert not source_metadata.exists()


def test_numpy_dependency_upper_bound_keeps_supported_mypy_stub_syntax() -> None:
    repository = Path(__file__).parents[2]
    project = tomllib.loads((repository / "pyproject.toml").read_text(encoding="utf-8"))

    assert "numpy>=2,<2.5" in project["project"]["dependencies"]

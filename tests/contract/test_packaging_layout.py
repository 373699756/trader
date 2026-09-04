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


def test_repository_ignores_history_and_training_intermediates_but_allows_final_artifacts() -> None:
    repository = Path(__file__).parents[2]
    ignore = (repository / ".gitignore").read_text(encoding="utf-8")

    assert "/data/history/" in ignore
    assert "/data/train/**/*" in ignore
    assert "!/data/train/**/model.json" in ignore
    assert "!/data/train/**/report.json" in ignore
    assert all(pattern in ignore for pattern in ("build/", "dist/", "*.egg-info/"))

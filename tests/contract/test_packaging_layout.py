from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_setuptools_egg_info_is_created_in_hidden_root_directory() -> None:
    repository = Path(__file__).parents[2]
    root_metadata = repository / ".egg-info" / "trader_research_dashboard.egg-info"
    source_metadata = repository / "src" / "trader_research_dashboard.egg-info"

    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-deps", "--no-build-isolation", "-e", "."],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )

    assert root_metadata.is_dir()
    assert not source_metadata.exists()

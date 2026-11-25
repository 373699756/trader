"""Retired manual price repair commands fail before composition or environment setup."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from trader.entrypoints import cli

ROOT = Path(__file__).resolve().parents[4]


@pytest.mark.parametrize("arguments", (
    ("history", "fill"), ("market", "fill"), ("company", "fill"),
    ("history", "--repair-codes", "300118"),
    ("company", "fill", "--repair-codes", "300118"),
))
def test_retired_price_repair_is_rejected_before_composition(monkeypatch, arguments):
    def forbidden(*args, **kwargs):
        pytest.fail("retired commands must not reach configuration or suppliers")

    monkeypatch.setattr(cli, "_run_sync", forbidden)
    with pytest.raises(SystemExit) as caught:
        cli.Main(["sync", *arguments])
    assert caught.value.code == 2


@pytest.mark.parametrize("target", ("history", "market", "company"))
def test_shell_launcher_rejects_retired_fill_before_environment_setup(tmp_path, target):
    missing_venv = tmp_path / "missing-venv"
    completed = subprocess.run(
        ("bash", str(ROOT / "run.sh"), "sync", target, "fill"),
        env={**os.environ, "VENV_DIR": str(missing_venv)},
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "用法：./run.sh sync [history|company]。\n"
    assert not missing_venv.exists()

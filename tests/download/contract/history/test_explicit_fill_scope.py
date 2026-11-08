"""Explicit repair selection crosses CLI and launcher boundaries without narrowing publication."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from trader.download.domain.price_repair import ValidateRepairSelection
from trader.entrypoints import cli

ROOT = Path(__file__).resolve().parents[4]


@pytest.mark.parametrize("target", ("history", "market"))
def test_cli_accepts_explicit_repair_selection_without_running_suppliers(monkeypatch, target):
    received = []
    monkeypatch.setattr(cli, "_run_sync", lambda args, progress: received.append(args) or 0)

    assert cli.Main(["sync", target, "--fill", "--repair-codes", "300118", "002129"]) == 0
    assert received[0].target == target
    assert received[0].fill
    assert received[0].repair_codes == ["300118", "002129"]


@pytest.mark.parametrize("arguments", (
    ("history", "--repair-codes", "300118"),
    ("company", "--fill", "--repair-codes", "300118"),
    ("history", "--fill", "--repair-codes", "300118", "300118"),
    ("history", "--fill", "--repair-codes", "３００１１８"),
    ("history", "--fill", "--repair-codes", "bad"),
    ("market", "--fill", "--repair-codes", "000001", "000002", "000003", "000004", "000005", "000006"),
))
def test_invalid_repair_scope_is_rejected_before_composition(monkeypatch, arguments):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid scope must not reach configuration or suppliers")

    monkeypatch.setattr(cli, "_run_sync", forbidden)
    with pytest.raises(SystemExit) as caught:
        cli.Main(["sync", *arguments])
    assert caught.value.code == 2


def test_domain_repair_selection_keeps_unscoped_sync_and_rejects_mutable_scope():
    ValidateRepairSelection(None, fill=False, target="all")
    ValidateRepairSelection(frozenset({"300118"}), fill=True, target="history")
    with pytest.raises(ValueError, match="fill_requires_explicit_target"):
        ValidateRepairSelection(None, fill=True, target="all")
    with pytest.raises(ValueError, match="repair_codes_require"):
        ValidateRepairSelection(set(), fill=True, target="history")


@pytest.mark.parametrize("target", ("history", "market"))
def test_shell_launcher_forwards_bounded_repair_scope(tmp_path, target):
    executable = tmp_path / "venv" / "bin"
    executable.mkdir(parents=True)
    fake_python = executable / "python3"
    fake_python.write_text("#!/usr/bin/env bash\nif [[ ${1:-} == -c ]]; then printf '0.0\\n'; fi\n", encoding="utf-8")
    entrypoint = executable / "trader-cli"
    entrypoint.write_text("#!/usr/bin/env bash\nprintf 'cli:%s\\n' \"$*\"\n", encoding="utf-8")
    for path in (fake_python, entrypoint):
        path.chmod(0o755)
        newer = (ROOT / "pyproject.toml").stat().st_mtime + 10
        os.utime(path, (newer, newer))
    config = tmp_path / "runtime.json"
    completed = subprocess.run(
        ("bash", str(ROOT / "run.sh"), "sync", target, "--fill", "--repair-codes", "300118"),
        env={**os.environ, "VENV_DIR": str(executable.parent), "TRADER_CONFIG": str(config)},
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == f"cli:--config {config} sync {target} --fill --repair-codes 300118"

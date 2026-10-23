"""Isolation and explicit invocation of the all-profile alignment probe."""

from pathlib import Path

import pytest

from scripts.diagnose_runtime import _parser, _validate, build_commands
from scripts.runtime_diagnostics.training_alignment import _probe


def test_all_profile_probe_requires_external_isolated_history_and_new_output(tmp_path: Path):
    with pytest.raises(ValueError, match="requires --history-root"):
        _validate(_parser().parse_args(["--profile", "training-alignment"]))
    args = [
        "--profile",
        "training-alignment",
        "--history-root",
        str(tmp_path / "history"),
        "--training-probe-directory",
        str(tmp_path / "probe"),
    ]
    options, _ = _validate(_parser().parse_args(args))
    commands = build_commands(options)
    assert commands[0].name == "training_alignment"
    assert "scripts.runtime_diagnostics.training_alignment" in commands[0].argv
    with pytest.raises(ValueError, match="external_history_copy"):
        _probe(Path(__file__).resolve().parents[3] / "data/market/history", tmp_path / "probe")
    with pytest.raises(ValueError, match="new_separate_output"):
        _probe(tmp_path / "history", tmp_path)

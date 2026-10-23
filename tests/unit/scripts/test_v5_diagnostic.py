"""The opt-in V5 probe requires an explicit external output directory."""

from pathlib import Path

import pytest

from scripts.diagnose_runtime import _parser, _validate


def test_v5_probe_requires_explicit_external_directory(tmp_path: Path):
    with pytest.raises(ValueError, match="v5-training requires"):
        _validate(_parser().parse_args(["--profile", "v5-training"]))
    options, _output = _validate(
        _parser().parse_args(["--profile", "v5-training", "--v5-probe-directory", str(tmp_path / "probe")])
    )
    assert options.v5_probe_directory == tmp_path / "probe"
    assert options.profile == "v5-training"

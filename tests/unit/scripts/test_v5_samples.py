
from tests.helpers.training_resources import TrainingSampleSettingsFixture
from scripts.checks.catalog import SelectionArguments

from tests.helpers.configuration import SqlitePolicyFixture
import hashlib
from pathlib import Path

import pytest

from scripts.checks.arguments import BuildParser
from scripts.checks.arguments import ValidateOptions
from scripts.checks.commands import BuildCommands
from scripts.checks.training.v5_samples import ProbeV5Samples


def test_isolated_disk_benchmark_and_read_only_real_input_replay(tmp_path):
    report = ProbeV5Samples(tmp_path / "synthetic", days=64, codes=2, sqlite_policy=SqlitePolicyFixture(), sample_settings=TrainingSampleSettingsFixture())
    assert report["status"] == "passed"
    assert report["summary"]["rows"] == 128
    assert report["summary"]["sqlite_bytes_ratio"] < 0.5
    assert report["summary"]["equal_values_and_order"] is True
    source = tmp_path / "synthetic/wide/samples.sqlite3"
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    replay = ProbeV5Samples(tmp_path / "replay", wide_source=source, sqlite_policy=SqlitePolicyFixture(), sample_settings=TrainingSampleSettingsFixture())
    assert replay["status"] == "passed"
    assert replay["summary"]["synthetic"] is False
    assert replay["summary"]["rows"] == 128
    assert replay["summary"]["equal_values_and_order"] is True
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    with pytest.raises(ValueError, match="new_external_directory"):
        ProbeV5Samples(tmp_path / "replay", sqlite_policy=SqlitePolicyFixture(), sample_settings=TrainingSampleSettingsFixture())


def test_sample_probe_routes_explicit_source_and_output(tmp_path):
    args = BuildParser().parse_args(
        [
            *SelectionArguments('v5-samples'),
            "--training-probe-directory",
            str(tmp_path),
            "--wide-sample-source",
            str(tmp_path / "source.sqlite3"),
        ]
    )
    options, _ = ValidateOptions(args)
    (command,) = BuildCommands(options, python_executable="/python3")
    assert command.name == "v5_samples"
    assert command.argv[-2:] == ("--wide-sample-source", str(tmp_path / "source.sqlite3"))
    assert command.argv[command.argv.index("--work-root") + 1] == str(tmp_path)


def test_sample_probe_requires_explicit_external_work_directory():
    with pytest.raises(ValueError, match="training-probe-directory"):
        ValidateOptions(BuildParser().parse_args([*SelectionArguments('v5-samples')]))
    with pytest.raises(ValueError, match="requires --check v5-samples"):
        ValidateOptions(BuildParser().parse_args([*SelectionArguments('disk'), "--wide-sample-source", str(Path("source"))]))

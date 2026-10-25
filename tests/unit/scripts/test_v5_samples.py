import hashlib
from pathlib import Path

import pytest

from scripts.diagnose_runtime import _parser, _validate, build_commands
from scripts.runtime_diagnostics.v5_samples import ProbeV5Samples


def test_isolated_storage_benchmark_and_read_only_real_input_replay(tmp_path):
    report = ProbeV5Samples(tmp_path / "synthetic", days=64, codes=2)
    assert report["status"] == "passed"
    assert report["summary"]["rows"] == 128
    assert report["summary"]["sqlite_bytes_ratio"] < 0.5
    assert report["summary"]["equal_values_and_order"] is True
    source = tmp_path / "synthetic/wide/samples.sqlite3"
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    replay = ProbeV5Samples(tmp_path / "replay", wide_source=source)
    assert replay["status"] == "passed"
    assert replay["summary"]["synthetic"] is False
    assert replay["summary"]["rows"] == 128
    assert replay["summary"]["equal_values_and_order"] is True
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    with pytest.raises(ValueError, match="new_external_directory"):
        ProbeV5Samples(tmp_path / "replay")


def test_sample_probe_routes_explicit_source_and_output(tmp_path):
    args = _parser().parse_args(
        [
            "--profile",
            "v5-samples",
            "--training-probe-directory",
            str(tmp_path),
            "--wide-sample-source",
            str(tmp_path / "source.sqlite3"),
        ]
    )
    options, _ = _validate(args)
    (command,) = build_commands(options, python_executable="/python3")
    assert command.name == "v5_samples"
    assert command.argv[-2:] == ("--wide-sample-source", str(tmp_path / "source.sqlite3"))
    assert command.argv[command.argv.index("--work-root") + 1] == str(tmp_path)


def test_sample_probe_requires_explicit_external_work_directory():
    with pytest.raises(ValueError, match="training-probe-directory"):
        _validate(_parser().parse_args(["--profile", "v5-samples"]))
    with pytest.raises(ValueError, match="requires --profile v5-samples"):
        _validate(_parser().parse_args(["--profile", "storage", "--wide-sample-source", str(Path("source"))]))

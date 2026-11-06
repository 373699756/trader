"""Useful worker controls remain available through explicit business actions."""

import pytest

from scripts.download.history import cli as history_cli, commands as history_commands
from scripts.download.market import cli as market_cli, commands as market_commands
from scripts.training import cli as training_cli, commands as training_commands


def _history(identity, arguments):
    options = history_cli.ParseOptions(history_cli.BuildParser(identity).parse_args(arguments))
    command, = history_commands.BuildCommands(options)
    return command


def _value(command, flag):
    return command.argv[command.argv.index(flag) + 1]


def test_daily_capability_preserves_defaults_and_forwards_explicit_controls():
    default = _history("history-daily-capability", [])
    assert [_value(default, flag) for flag in ("--universe-size", "--code", "--days", "--timeout-seconds")] == [
        "5453", "600519", "120", "8.0",
    ]
    selected = _history("history-daily-capability", [
        "--universe-size", "6000", "--code", "000001", "--days", "90", "--timeout-seconds", "12",
    ])
    assert [_value(selected, flag) for flag in ("--universe-size", "--code", "--days", "--timeout-seconds")] == [
        "6000", "000001", "90", "12.0",
    ]


@pytest.mark.parametrize(
    "arguments",
    (
        ("--universe-size", "0"),
        ("--code", "bad"),
        ("--days", "0"),
        ("--timeout-seconds", "0"),
        ("--timeout-seconds", "nan"),
    ),
)
def test_daily_capability_rejects_invalid_controls(arguments):
    with pytest.raises(ValueError):
        _history("history-daily-capability", list(arguments))


def test_history_write_preserves_public_workload_defaults_and_forwards_flush_pair():
    default = _history("history-write", [])
    assert _value(default, "--size") == "10" and _value(default, "--sessions") == "61"
    assert "--flush-codes" not in default.argv and "--comparison-rounds" not in default.argv
    selected = _history("history-write", ["--flush-codes", "8", "--flush-cells", "512"])
    assert _value(selected, "--flush-codes") == "8" and _value(selected, "--flush-cells") == "512"


def test_history_comparison_rounds_require_and_reach_the_baseline_mode(tmp_path):
    command = _history("history-write", ["--baseline-checkout", str(tmp_path / "baseline"), "--comparison-rounds", "7"])
    assert _value(command, "--comparison-rounds") == "7"
    assert not (tmp_path / "baseline").exists()


@pytest.mark.parametrize(
    "arguments",
    (
        ("--comparison-rounds", "5"),
        ("--flush-codes", "8"),
        ("--flush-cells", "512"),
        ("--flush-codes", "0", "--flush-cells", "512"),
        ("--flush-codes", "65", "--flush-cells", "512"),
        ("--flush-codes", "8", "--flush-cells", "32769"),
    ),
)
def test_history_write_rejects_incomplete_or_ineffective_controls(arguments):
    with pytest.raises(ValueError):
        _history("history-write", list(arguments))


def test_flush_controls_cannot_be_lost_in_comparison_or_published_replay(tmp_path):
    for arguments in (
        ["--baseline-checkout", str(tmp_path / "baseline")],
        ["--history-publication-profile", "--history-root", str(tmp_path / "history")],
    ):
        with pytest.raises(ValueError, match="synthetic history writer"):
            _history("history-write", [*arguments, "--flush-codes", "8", "--flush-cells", "512"])


def test_parallel_baostock_interval_is_forwarded_and_serial_mode_is_separate():
    default = _history("baostock-concurrency", [])
    assert _value(default, "--interval-seconds") == "2.0"
    selected = _history("baostock-concurrency", ["--interval-seconds", "3.5"])
    assert _value(selected, "--interval-seconds") == "3.5"
    serial = _history("baostock-concurrency", ["--baostock-serial-only"])
    assert "--interval-seconds" not in serial.argv and "--intervals" in serial.argv
    with pytest.raises(SystemExit):
        history_cli.BuildParser("baostock-concurrency").parse_args(["--baostock-serial-only", "--interval-seconds", "2"])
    for interval in ("1.9", "nan", "inf"):
        with pytest.raises(ValueError, match="at least two"):
            _history("baostock-concurrency", ["--interval-seconds", interval])


def test_security_master_minimum_rows_is_positive_and_preserves_default():
    default_options = market_cli.ParseOptions(market_cli.BuildParser("security-master").parse_args([]))
    default, = market_commands.BuildCommands(default_options)
    assert _value(default, "--minimum-rows") == "4000"
    options = market_cli.ParseOptions(market_cli.BuildParser("security-master").parse_args(["--minimum-rows", "5000"]))
    selected, = market_commands.BuildCommands(options)
    assert _value(selected, "--minimum-rows") == "5000"
    with pytest.raises(ValueError, match="positive"):
        market_cli.ParseOptions(market_cli.BuildParser("security-master").parse_args(["--minimum-rows", "0"]))


def test_training_sample_workload_controls_preserve_defaults_and_are_forwarded(tmp_path):
    base = [
        "--training-probe-directory", str(tmp_path / "probe"),
        "--baseline-checkout", str(tmp_path / "baseline"),
    ]
    default_options = training_cli.ParseOptions(training_cli.BuildParser("training-samples").parse_args(base))
    default, = training_commands.BuildCommands(default_options)
    assert _value(default, "--size") == "64" and _value(default, "--sessions") == "750"
    options = training_cli.ParseOptions(training_cli.BuildParser("training-samples").parse_args([
        *base, "--size", "4", "--sessions", "64",
    ]))
    selected, = training_commands.BuildCommands(options)
    assert _value(selected, "--size") == "4" and _value(selected, "--sessions") == "64"
    assert not (tmp_path / "probe").exists()


@pytest.mark.parametrize("arguments", (("--size", "3"), ("--size", "129"), ("--sessions", "63"), ("--sessions", "751")))
def test_training_sample_workload_rejects_out_of_range_controls(tmp_path, arguments):
    with pytest.raises(ValueError, match="sample comparison"):
        training_cli.ParseOptions(training_cli.BuildParser("training-samples").parse_args([
            "--training-probe-directory", str(tmp_path / "probe"),
            "--baseline-checkout", str(tmp_path / "baseline"), *arguments,
        ]))


def test_worker_controls_are_not_accepted_by_other_actions():
    with pytest.raises(SystemExit):
        history_cli.BuildParser("history").parse_args(["--flush-codes", "8"])
    with pytest.raises(SystemExit):
        market_cli.BuildParser("tencent").parse_args(["--minimum-rows", "5000"])
    with pytest.raises(SystemExit):
        training_cli.BuildParser("training-resources").parse_args(["--sessions", "64"])

"""Structured-source modes retain their native input and budget contracts."""

from datetime import date

import pytest

from scripts.download.company.cli import BuildParser, ParseOptions
from scripts.download.company.commands import BuildCommands


def _parse(arguments):
    return ParseOptions(BuildParser("research-source-compare").parse_args(arguments))


@pytest.mark.parametrize("rows", (1, 5, 500))
def test_structured_field_mode_passes_rows_and_exact_source_budgets(rows):
    options = _parse([
        "--field-sources-only", "--field-source-rows", str(rows),
        "--source-timeout-seconds", "12", "--market-source-loop-seconds", "60",
        "--market-task-seconds", "300", "--command-timeout-seconds", "400",
    ])
    command, = BuildCommands(options, python_executable="/python3")
    assert command.name == "research_source_compare"
    assert "--field-sources-only" in command.argv
    assert command.argv[command.argv.index("--field-source-rows") + 1] == str(rows)
    assert command.argv[command.argv.index("--timeout-seconds") + 1] == "12.0"
    assert command.argv[command.argv.index("--source-loop-seconds") + 1] == "60.0"
    assert command.argv[command.argv.index("--task-seconds") + 1] == "300.0"
    assert command.timeout_seconds == 300


def test_structured_schema_and_identities_are_forwarded_only_when_explicit():
    options = _parse([
        "--field-sources-only", "--field-source-rows", "1",
        "--field-source-schema", "--field-source-identities",
    ])
    command, = BuildCommands(options)
    assert "--field-source-schema" in command.argv and "--field-source-identities" in command.argv
    default, = BuildCommands(_parse([]))
    assert "--field-sources-only" not in default.argv and "--field-source-schema" not in default.argv
    assert "--field-source-identities" not in default.argv and "--annual-income-period" not in default.argv


@pytest.mark.parametrize(
    "arguments",
    (
        ("--field-source-rows", "0"),
        ("--field-source-rows", "501", "--field-sources-only"),
        ("--field-source-rows", "2"),
        ("--field-source-schema",),
        ("--field-sources-only", "--field-source-schema", "--field-source-rows", "2"),
        ("--field-source-identities",),
        ("--field-sources-only", "--field-source-identities", "--field-source-rows", "6"),
        ("--source-timeout-seconds", "31"),
        ("--market-source-loop-seconds", "121"),
        ("--market-task-seconds", "601"),
    ),
)
def test_structured_source_mode_preserves_native_validation(arguments):
    with pytest.raises(ValueError):
        _parse(list(arguments))


def test_annual_income_mode_passes_one_code_and_a_december_period():
    options = _parse(["--codes", "600001", "--annual-income-period", "2025-12-31"])
    assert options.annual_income_period == date(2025, 12, 31)
    command, = BuildCommands(options)
    assert command.argv[-2:] == ("--annual-income-period", "2025-12-31")
    position = command.argv.index("--codes")
    assert command.argv[position + 1] == "600001" and command.argv[position + 2].startswith("--")


@pytest.mark.parametrize(
    "arguments",
    (
        ("--codes", "600001", "000001", "--annual-income-period", "2025-12-31"),
        ("--codes", "600001", "600001", "--annual-income-period", "2025-12-31"),
        ("--codes", "600001", "--annual-income-period", "2025-06-30"),
        ("--codes", "60001", "--annual-income-period", "2025-12-31"),
    ),
)
def test_annual_income_mode_requires_native_single_code_and_period_contract(arguments):
    with pytest.raises(ValueError):
        _parse(list(arguments))


def test_annual_and_structured_modes_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        BuildParser("research-source-compare").parse_args([
            "--field-sources-only", "--annual-income-period", "2025-12-31", "--codes", "600001",
        ])


def test_company_coverage_rejects_source_mode_arguments():
    with pytest.raises(SystemExit):
        BuildParser("company-resume").parse_args(["--field-sources-only"])


@pytest.mark.parametrize("arguments", (
    ["--codes", "600001"], ["--field-sources-only", "--codes", "600001"],
    ["--codes", "600519", "300750", "688981"],
    ["--field-sources-only", "--codes", "600519", "300750", "688981"],
))
def test_nonannual_source_modes_reject_ineffective_security_selection(arguments):
    with pytest.raises(ValueError, match="only with --annual-income-period"):
        _parse(arguments)


@pytest.mark.parametrize("arguments", ([], ["--field-sources-only"]))
def test_nonannual_source_modes_have_no_default_or_worker_security_selection(arguments):
    options = _parse(arguments)
    assert options.codes == ()
    command, = BuildCommands(options)
    assert "--codes" not in command.argv


def test_annual_income_mode_requires_an_explicit_security():
    with pytest.raises(ValueError, match="requires one code"):
        _parse(["--annual-income-period", "2025-12-31"])


@pytest.mark.parametrize("period", ("2026-03-31", "2026-06-30", "2026-09-30", "2025-12-31"))
def test_financial_revision_mode_accepts_one_stock_and_all_quarter_ends(period):
    options = _parse(["--codes", "600001", "--financial-revision-period", period])
    assert options.financial_revision_period == date.fromisoformat(period)
    command, = BuildCommands(options)
    assert command.argv[-4:] == ("--codes", "600001", "--financial-revision-period", period)
    assert "--annual-income-period" not in command.argv and "--field-sources-only" not in command.argv


@pytest.mark.parametrize("arguments", (
    ["--financial-revision-period", "2026-06-30"],
    ["--codes", "600001", "000001", "--financial-revision-period", "2026-06-30"],
    ["--codes", "60001", "--financial-revision-period", "2026-06-30"],
    ["--codes", "600001", "--financial-revision-period", "2026-06-29"],
    ["--codes", "600001", "--financial-revision-period", "2026-06-30", "--field-source-schema"],
))
def test_financial_revision_mode_refuses_ambiguous_or_invalid_selection(arguments):
    with pytest.raises(ValueError):
        _parse(arguments)


@pytest.mark.parametrize("other", (["--field-sources-only"], ["--annual-income-period", "2025-12-31"]))
def test_financial_revision_mode_is_mutually_exclusive_with_all_other_source_modes(other):
    with pytest.raises(SystemExit):
        _parse(["--codes", "600001", "--financial-revision-period", "2026-06-30", *other])

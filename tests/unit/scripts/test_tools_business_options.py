"""Business tool boundaries use selected configuration and explicit audit modes."""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.download.company.cli import BuildParser, ParseOptions
from scripts.download.company.commands import BuildCommands
from scripts.infra.database.database_layout_comparison import _annual_history_configuration
from scripts.infra.database.database_workspace import DatabaseProbeLocation
from scripts.training.samples import training_read_comparison, training_samples
from tests.helpers.configuration import HistorySyncConfigurationFixture, SqlitePolicyFixture
from trader.infra.settings import LoadRuntimeSettings


def _company_arguments(tmp_path: Path, mode: str) -> list[str]:
    return [
        mode,
        "--company-replay-directory", str(tmp_path / "replay"),
        "--company-collection-database", str(tmp_path / "collection.sqlite3"),
        "--company-evidence-database", str(tmp_path / "facts.sqlite3"),
        "--command-timeout-seconds", "180",
        "--company-audit-budget-seconds", "120",
    ]


@pytest.mark.parametrize(
    ("mode", "native"),
    (
        ("--company-pacing-audit", "--pacing-only"),
    ),
)
def test_company_isolated_modes_keep_directory_and_exact_worker_budget(tmp_path, mode, native):
    options = ParseOptions(BuildParser("company-resume").parse_args(_company_arguments(tmp_path, mode)))
    command, = BuildCommands(options, python_executable="/python3")
    assert native in command.argv and "--live-sync" not in command.argv
    assert command.argv[command.argv.index("--directory") + 1] == str(tmp_path / "replay")
    assert command.argv[command.argv.index("--budget-seconds") + 1] == "120.0"
    assert command.timeout_seconds == 180
    assert not (tmp_path / "replay").exists()


@pytest.mark.parametrize("mode", ("--company-pacing-audit",))
def test_company_isolated_modes_require_explicit_external_directory(mode):
    with pytest.raises(ValueError, match="company-replay-directory"):
        ParseOptions(BuildParser("company-resume").parse_args([mode]))
    with pytest.raises(ValueError, match="outside the repository"):
        ParseOptions(BuildParser("company-resume").parse_args([
            mode, "--company-replay-directory", str(Path.cwd() / "data/replay"),
        ]))


def test_company_material_keys_are_read_only_and_pass_the_facts_input(tmp_path):
    options = ParseOptions(BuildParser("company-resume").parse_args([
        "--company-material-keys", "notice-a", "notice-b",
        "--company-collection-database", str(tmp_path / "collection.sqlite3"),
        "--company-evidence-database", str(tmp_path / "facts.sqlite3"),
    ]))
    command, = BuildCommands(options, python_executable="/python3")
    assert command.argv[-3:] == ("--material-keys", "notice-a", "notice-b")
    assert command.argv[command.argv.index("--facts-database") + 1] == str(tmp_path / "facts.sqlite3")
    assert "--directory" not in command.argv and "--live-sync" not in command.argv
    assert options.company_material_keys == ("notice-a", "notice-b")


@pytest.mark.parametrize(
    "keys",
    (
        ("duplicate", "duplicate"),
        ("contains space",),
        ("",),
        ("a", "b", "c", "d", "e", "f", "g"),
    ),
)
def test_company_material_keys_reject_invalid_source_selections(keys):
    with pytest.raises(ValueError, match="one to six"):
        ParseOptions(BuildParser("company-resume").parse_args(["--company-material-keys", *keys]))


@pytest.mark.parametrize(
    "arguments",
    (
        ("--company-pacing-audit", "--company-pipeline-audit"),
        ("--company-pipeline-audit", "--company-material-keys", "notice"),
    ),
)
def test_company_modes_are_mutually_exclusive(arguments):
    with pytest.raises(SystemExit):
        BuildParser("company-resume").parse_args(list(arguments))


@pytest.mark.parametrize(
    "arguments",
    (
        ("--command-timeout-seconds", "100", "--company-audit-budget-seconds", "90"),
        ("--command-timeout-seconds", "30"),
        ("--company-audit-budget-seconds", "nan"),
        ("--company-audit-budget-seconds", "29"),
    ),
)
def test_company_budget_cannot_exceed_the_actual_command_window(arguments):
    with pytest.raises(ValueError, match="audit budget"):
        ParseOptions(BuildParser("company-resume").parse_args(["--company-database-audit", *arguments]))


def test_training_read_worker_loads_explicit_runtime_and_separates_budget_digest(tmp_path, monkeypatch, capsys):
    runtime = tmp_path / "selected/runtime.json"
    runtime.parent.mkdir()
    runtime.write_text('{"fixture":"selected runtime"}', encoding="utf-8")
    performance = runtime.with_name("performance.json")
    performance.write_text('{"fixture":"selected budget"}', encoding="utf-8")
    settings = object()
    policy = SqlitePolicyFixture()
    selected = []
    monkeypatch.setattr("trader.infra.settings.LoadRuntimeSettings", lambda path: selected.append(path) or settings)
    monkeypatch.setattr("trader.bootstrap.BuildSqlitePolicy", lambda received: policy if received is settings else None)

    def preparation(history, output, mode, *, sqlite_policy):
        assert sqlite_policy is policy and mode == "sqlite"
        return {"cases": {}, "peak_rss_bytes": 0}

    monkeypatch.setattr(training_read_comparison, "MeasurePreparation", preparation)
    monkeypatch.setattr(sys, "argv", [
        "training-read", "--history", str(tmp_path / "history"), "--output", str(tmp_path / "output"),
        "--mode", "sqlite", "--runtime-config", str(runtime),
    ])
    assert training_read_comparison.Main() == 0
    report = json.loads(capsys.readouterr().out)
    assert selected == [runtime]
    assert report["runtime_config_hash"] == hashlib.sha256(runtime.read_bytes()).hexdigest()
    assert report["performance_budget_hash"] == hashlib.sha256(performance.read_bytes()).hexdigest()
    assert report["runtime_config_hash"] != report["performance_budget_hash"]
    assert not (tmp_path / "output").exists()


def test_training_sample_probe_passes_runtime_to_every_nested_worker(tmp_path, monkeypatch):
    runtime = tmp_path / "selected/runtime.json"
    runtime.parent.mkdir()
    runtime.write_text('{"fixture":"runtime"}', encoding="utf-8")
    performance = runtime.with_name("performance.json")
    performance.write_text('{"fixture":"budget"}', encoding="utf-8")
    runtime_hash = hashlib.sha256(runtime.read_bytes()).hexdigest()
    performance_hash = hashlib.sha256(performance.read_bytes()).hexdigest()
    settings = LoadRuntimeSettings(Path(__file__).resolve().parents[3] / "config/runtime.json")
    budget = settings.performance_budgets
    settings = replace(settings, performance_budgets=replace(
        budget,
        rounds=replace(budget.rounds, measurement=1),
        database=replace(budget.database, hdd=replace(budget.database.hdd, evidence_status="validated")),
    ))
    work = tmp_path / "work"
    work.mkdir()
    baseline = tmp_path / "baseline"
    (baseline / "src/trader").mkdir(parents=True)
    monkeypatch.setattr(training_samples, "LoadRuntimeSettings", lambda selected: settings)
    monkeypatch.setattr(training_samples, "PrepareDatabaseProbeDirectory", lambda *_: DatabaseProbeLocation(
        work, "hdd", "fixture", "fixture", False,
    ))
    monkeypatch.setattr(training_samples, "PrepareTrainingHistory", lambda *_args, **_kwargs: tmp_path / "history")
    commands = []

    def run(argv, cwd, directory):
        commands.append(argv)
        assert argv[argv.index("--runtime-config") + 1] == str(runtime)
        result = {
            "cases": {
                name: {"seconds": 1.0, "numeric_hash": "fixture-identity", "rows": 4}
                for name in ("initial", "repeat", "incremental")
            },
            "peak_rss_bytes": 128,
        }
        if "scripts.training.samples.training_read_comparison" in argv:
            result.update(runtime_config_hash=runtime_hash, performance_budget_hash=performance_hash)
        return result

    monkeypatch.setattr(training_samples, "_run", run)
    report = training_samples.ProbeTrainingSamples(
        work, baseline, runtime, size=4, sessions=64, sqlite_policy=SqlitePolicyFixture(),
    )
    assert len(commands) == 4
    assert report["summary"]["runtime_config_hash"] == runtime_hash
    assert report["summary"]["performance_budget_hash"] == performance_hash
    assert "config_hash" not in report["summary"]
    assert not (tmp_path / "history").exists()


def test_annual_comparison_configuration_uses_the_selected_checkout_defaults(tmp_path, monkeypatch):
    checkout = tmp_path / "selected-checkout"
    root, history = tmp_path / "work", tmp_path / "specific-history"
    configured = HistorySyncConfigurationFixture(tmp_path / "configured-history", tmp_path / "configured-reference")
    selected = []

    def load(received_checkout, paths):
        selected.append((received_checkout, paths.root))
        return configured

    monkeypatch.setattr("trader.download.infra.download_settings.LoadHistorySyncConfiguration", load)
    actual = _annual_history_configuration(checkout, root, history)
    assert selected == [(checkout, root)]
    assert actual.history_root == history and actual.reference_root == root / "reference"
    assert actual.sessions == 2000 and actual.minimum_free_bytes == 0
    assert actual.reread_sessions == configured.reread_sessions
    assert actual.download_batch_size == configured.download_batch_size
    assert actual.history_workers == configured.history_workers
    assert actual.supplier_timeout_seconds == configured.supplier_timeout_seconds
    assert actual.write_batches == configured.write_batches

from __future__ import annotations

from scripts.checks.catalog import SelectionArguments

from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from scripts.checks.contracts import DiagnosticCommand
from scripts.checks.contracts import DiagnosticOptions
from scripts.checks.contracts import DiagnosticResult
from scripts.checks.arguments import BuildParser
from scripts.checks.arguments import ValidateOptions
from scripts.checks.commands import BuildCommands
from scripts.checks.projections import BuildReport
from scripts.checks.execution import ExecuteCommand
from scripts.checks.execution import RunDiagnostics
from scripts.checks.download.history import baostock_concurrency
from scripts.checks.recommendation.browser_refresh import _seed
from scripts.checks.download.history.history_sqlite_performance import inspect_history_space
from trader.download.infra.baostock_session import RateLimitedBaoStockSdk
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import UnifiedFreezeIndex
from trader.recommendation.application.runtime.schedule import SHANGHAI
from trader.recommendation.domain.publication.models import Strategy


def _options(**overrides: object) -> DiagnosticOptions:
    defaults = DiagnosticOptions(
        check_id="web",
        base_url="http://127.0.0.1:5000",
        runtime_config=Path("config/runtime.json"),
        codes=("600519", "300750", "688981"),
        web_samples=3,
        web_interval_seconds=2.0,
        source_samples=1,
        source_interval_seconds=1.0,
        history_workers=3,
        history_days=61,
        history_source="composite",
        tencent_history_host="proxy",
        web_timeout_seconds=3.0,
        source_timeout_seconds=4.5,
        browser_duration_seconds=8.0,
        browser_minimum_updates=3,
        command_timeout_seconds=180.0,
        history_root=Path("data/history"),
        sqlite_page_sample_count=1,
        sqlite_query_rounds=3,
        sqlite_revision_write_sample_count=512,
    )
    return replace(defaults, **overrides)


def test_removed_index_experiment_is_rejected_before_execution(capsys):
    parser = BuildParser()
    assert "history-indexes" not in parser.format_help()
    with pytest.raises(SystemExit) as failure:
        parser.parse_args(["--profile", "history-indexes"])
    assert failure.value.code == 2
    assert "required" in capsys.readouterr().err


def test_price_repair_audit_can_resume_its_external_copy_without_empty_directory_preparation():
    options, _ = ValidateOptions(BuildParser().parse_args([*SelectionArguments('price-repair-audit'), "--history-root", "data/market/history", "--database-probe-directory", "/tmp/trader-price-repair-test"]))
    commands = BuildCommands(options, python_executable="/python3")
    assert len(commands) == 1
    assert commands[0].name == "price_repair_audit"
    assert commands[0].argv[-2:] == ("--work-root", "/tmp/trader-price-repair-test")
    assert commands[0].database_probe_directory is None


@pytest.mark.parametrize("profile", ("v1", "v2", "v3", "v5"))
def test_local_selected_profile_selects_only_offline_business_checks(profile):
    commands = BuildCommands(_options(check_id="snapshot", scoring_profile=profile), python_executable="/python")
    assert tuple(command.name for command in commands) == (
        f"configuration:{profile}",
        "history_maintenance",
        f"research_readiness:{profile}",
        f"production_performance:{profile}",
    )
    assert all("--profile" in command.argv for command in commands if command.name != "history_maintenance")
    assert all(
        not any(part in command.argv for part in ("serve", "sync", "train", "trader.entrypoints.server"))
        for command in commands
    )


@pytest.mark.parametrize("profile", ("v1", "v2", "v3", "v5"))
def test_runtime_profile_selection_reaches_each_read_only_web_probe(profile):
    options, _ = ValidateOptions(BuildParser().parse_args([*SelectionArguments('web'), "--scoring-profile", profile]))
    commands = BuildCommands(options, python_executable="/python")
    assert tuple(command.name for command in commands) == (f"web_health:{profile}",)
    assert commands[0].argv[-2:] == ("--profile", profile)


@pytest.mark.parametrize("selection", ("v1,v2,v3,v5", "v5,v3,v1,v2"))
def test_performance_four_profiles_share_one_child_command(selection: str) -> None:
    options, _ = ValidateOptions(BuildParser().parse_args([*SelectionArguments('performance'), "--scoring-profile", selection]))
    commands = BuildCommands(options, python_executable="/python")

    assert options.scoring_profile == "v1,v2,v3,v5"
    assert len(commands) == 1
    assert commands[0].name == "production_performance:v1,v2,v3,v5"
    assert commands[0].argv[:3] == ("/python", "-m", "trader.entrypoints.performance")
    assert commands[0].argv[-2:] == ("--profile", "v1,v2,v3,v5")


@pytest.mark.parametrize("profile", ("snapshot", "research", "web", "web", "browser"))
def test_four_profiles_are_rejected_outside_the_performance_diagnostic(profile: str) -> None:
    with pytest.raises(ValueError, match="only available for performance"):
        ValidateOptions(BuildParser().parse_args([*SelectionArguments(profile), "--scoring-profile", "v1,v2,v3,v5"]))


@pytest.mark.parametrize("selection", ("v1,v5", "v1,v2,v3,v3", "v1,v2,v3,v9", "v1,"))
def test_performance_rejects_partial_duplicate_or_invalid_profile_selections(selection: str) -> None:
    with pytest.raises(SystemExit) as error:
        BuildParser().parse_args([*SelectionArguments('performance'), "--scoring-profile", selection])
    assert error.value.code == 2


def test_continuous_performance_option_reaches_the_existing_child_and_report():
    options, _ = ValidateOptions(
        BuildParser().parse_args(
            [
                *SelectionArguments('performance'),
                "--scoring-profile",
                "v3",
                "--performance-replay-ticks",
                "100",
                "--performance-replay-rows",
                "120",
            ]
        )
    )
    command = BuildCommands(options, python_executable="/python")[0]
    position = command.argv.index("--replay-ticks")
    assert command.argv[position + 1] == "100"
    assert command.argv[command.argv.index("--replay-rows") + 1] == "120"
    result = DiagnosticResult(
        command.name,
        0,
        1.0,
        {
            "status": "passed",
            "continuous_pipeline": {"contract": "fixture-replay", "cold": {"wall_ms": 2}},
        },
        None,
    )
    report = BuildReport("performance", (result,))
    assert report["checks"][0]["summary"]["continuous_pipeline"]["contract"] == "fixture-replay"


@pytest.mark.parametrize(
    "arguments",
    (
        [*SelectionArguments('web'), "--performance-replay-ticks", "2"],
        [*SelectionArguments('performance'), "--performance-replay-ticks", "1001"],
        [*SelectionArguments('performance'), "--performance-replay-ticks", "-1"],
        [*SelectionArguments('performance'), "--performance-replay-rows", "120"],
    ),
)
def test_continuous_performance_option_rejects_wrong_profile_and_unbounded_runs(arguments):
    with pytest.raises(ValueError, match="performance-replay-"):
        ValidateOptions(BuildParser().parse_args(arguments))


@pytest.mark.parametrize("profile", ("snapshot", "research", "web", "web", "performance", "browser"))
def test_removed_both_alias_is_rejected_before_execution(profile) -> None:
    parser = BuildParser()
    assert "both" not in parser.format_help()
    with pytest.raises(SystemExit) as error:
        parser.parse_args([*SelectionArguments(profile), "--scoring-profile", "both"])
    assert error.value.code == 2


def test_only_the_performance_diagnostic_defaults_to_four_profiles() -> None:
    performance = BuildCommands(_options(check_id="performance"), python_executable="/python")
    local = BuildCommands(_options(check_id="snapshot"), python_executable="/python")

    assert len(performance) == 1
    assert performance[0].argv[-2:] == ("--profile", "v1,v2,v3,v5")
    local_performance = next(command for command in local if command.name == "production_performance")
    assert "--profile" not in local_performance.argv


def test_four_profile_performance_report_retains_shared_process_and_model_evidence() -> None:
    evidence = {
        "workload": {"profiles": ["v1", "v2", "v3", "v5"], "model_heads": 8, "execution_mode": "same_process"},
        "identity": {"source_hash": "source", "profiles": {"v5": {"model_hash": "model"}}},
        "network_calls": 0,
        "model_workload": {"unchanged": {"predictor_calls": 8, "elapsed_ms": 12}},
        "profile_results": {"v5": {"status": "passed", "head_count": 2}},
        "measurements": {"quote_to_draft": {"p95_ms": 12}},
        "memory": {"peak_rss_bytes": 1024},
        "failures": [],
    }
    report = BuildReport(
        "performance",
        (
            DiagnosticResult(
                "production_performance:v1,v2,v3,v5", 0, 12.0, {"passed": True, **evidence, "private": "omit"}, None
            ),
        ),
    )

    assert report["status"] == "passed"
    assert report["checks"][0]["summary"] == evidence


def test_history_resume_probe_routes_sample_bound_without_starting_downloads():
    commands = BuildCommands(_options(check_id="history-sqlite", history_resume_samples=8), python_executable="/python")
    assert len(commands) == 1
    command = commands[0]
    assert command.argv[command.argv.index("--resume-samples") + 1] == "8"
    assert "sync" not in command.argv and "train" not in command.argv
    args = BuildParser().parse_args([*SelectionArguments('history-sqlite'), "--history-resume-samples", "9"])
    with pytest.raises(ValueError, match="--history-resume-samples"):
        ValidateOptions(args)


def test_preload_timing_defaults_to_three_profiles_and_separates_feature_cpu_probe():
    plain, _ = ValidateOptions(BuildParser().parse_args([*SelectionArguments('history-sqlite'), "--history-preload-audit"]))
    command = BuildCommands(plain, python_executable="/python3")[0]
    assert command.argv[command.argv.index("--preload-profiles") + 1] == "v1,v2,v3"
    assert "--preload-cpu-profile" not in command.argv
    assert "--preload-feature-audit" not in command.argv
    detailed, _ = ValidateOptions(BuildParser().parse_args([
        *SelectionArguments('history-sqlite'), "--history-preload-audit", "--history-preload-feature-audit",
        "--history-preload-profiles", "v3",
    ]))
    command = BuildCommands(detailed, python_executable="/python3")[0]
    assert "--preload-feature-audit" in command.argv
    assert command.argv[command.argv.index("--preload-profiles") + 1] == "v3"


def test_model_inputs_forwards_the_selected_runtime_configuration():
    options = _options(check_id="model-inputs", runtime_config=Path("config/runtime.json"))
    command = BuildCommands(options, python_executable="/python3")[0]
    assert command.argv[command.argv.index("--runtime-config") + 1] == str(options.runtime_config)


def test_local_invalid_configuration_skips_dependents():
    commands = BuildCommands(_options(check_id="snapshot", scoring_profile="v2"), python_executable="/python")
    called = []

    def runner(command):
        called.append(command.name)
        return DiagnosticResult(command.name, 2, 0.0, {"status": "failed"}, None)

    report = RunDiagnostics("snapshot", commands, runner=runner)
    assert called == ["configuration:v2"]
    skipped = [item for item in report["checks"] if not item["name"].startswith("configuration")]
    assert len(skipped) == 3
    assert all(item["executed"] is False and item["reason"] == "configuration_invalid" for item in skipped)


def test_local_research_insufficiency_is_degraded_but_specialist_stays_strict():
    result = DiagnosticResult(
        "research_readiness",
        0,
        0.0,
        {
            "schema_version": "research_readiness",
            "production_authority": False,
            "history_archive": {
                "state": "unavailable",
                "reason": "history_snapshot_unavailable",
                "calendar_sessions": 0,
                "universe_count": 0,
                "partition_count": 0,
                "production_authority": False,
                "point_in_time_parity": False,
            },
            "tomorrow_research": {
                "status": "blocked",
                "production_readiness": "production_adaptation_blocked",
                "production_authority": False,
                "input_blockers": ["tomorrow_h1_historical_data_insufficient"],
                "production_blockers": ["production_adaptation_blocked"],
            },
        },
        None,
    )
    assert BuildReport("snapshot", (result,))["status"] == "degraded"
    assert BuildReport("research", (result,))["status"] == "failed"


def test_browser_diagnostic_seed_matches_the_current_freeze_item_contract() -> None:
    index = UnifiedFreezeIndex()
    observed_at = datetime(2026, 8, 24, 13, 5, tzinfo=SHANGHAI)

    _seed(index, Strategy.TOMORROW, observed_at, "600002")

    current = index.snapshot(Strategy.TOMORROW).current
    assert current is not None
    assert current.items[0].board.value == "main"
    assert current.items[0].selection_rank == 1


def test_web_selection_never_adds_supplier_probes() -> None:
    commands = BuildCommands(_options(), python_executable="/python3")
    assert tuple(command.name for command in commands) == ("web_health",)
    assert all("sync" not in command.argv and "train" not in command.argv for command in commands)


def test_cpu_profile_keeps_the_selected_diagnostic_and_external_output(tmp_path: Path) -> None:
    args = BuildParser().parse_args([*SelectionArguments('market-factor-compare'), "--cpu-profile-dir", str(tmp_path)])
    options, _ = ValidateOptions(args)
    command = BuildCommands(options, python_executable="python3")[0]

    assert command.name == "market_factor_compare"
    assert command.argv[:3] == ("python3", "-m", "scripts.diagnose_runtime")
    assert command.argv[-2:] == ("--cpu-profile-dir", str(tmp_path))
    with pytest.raises(ValueError, match="outside"):
        ValidateOptions(
            BuildParser().parse_args(
                [*SelectionArguments('market-factor-compare'), "--cpu-profile-dir", str(Path.cwd() / "profiles")]
            )
        )


def test_continuous_cpu_trace_modes_reach_child_and_reject_unbounded_capture(tmp_path):
    arguments = [
        *SelectionArguments('performance'),
        "--scoring-profile",
        "v2",
        "--performance-replay-ticks",
        "1",
        "--cpu-profile-dir",
        str(tmp_path),
        "--cpu-profile-mode",
        "gc",
    ]
    options, _ = ValidateOptions(BuildParser().parse_args(arguments))
    command = BuildCommands(options, python_executable="python3")[0]
    assert command.argv[command.argv.index("--cpu-profile-dir") + 1] == str(tmp_path)
    assert command.argv[command.argv.index("--cpu-profile-mode") + 1] == "gc"
    arguments[arguments.index("--performance-replay-ticks") + 1] = "2"
    with pytest.raises(ValueError, match="replay-ticks 1"):
        ValidateOptions(BuildParser().parse_args(arguments))


def test_tencent_download_profile_uses_actual_updater_with_bounded_samples():
    commands = BuildCommands(_options(check_id="tencent-download", download_sizes=(10, 50, 100)))
    assert len(commands) == 1 and commands[0].name == "tencent_download"
    argv = commands[0].argv
    assert "scripts.checks.download.history.tencent_download" in argv
    offset = argv.index("--sizes")
    assert argv[offset + 1 : offset + 4] == ("10", "50", "100")
    assert argv[argv.index("--workers") + 1] == "3"


def test_history_layout_comparison_requires_explicit_external_baseline(tmp_path):
    args = BuildParser().parse_args(
        [
            *SelectionArguments('history-write'),
            "--baseline-checkout",
            str(tmp_path),
            "--download-sizes",
            "5000",
        ]
    )
    options, _ = ValidateOptions(args)
    command = BuildCommands(options)[0]
    assert command.argv[-2:] == ("--baseline-checkout", str(tmp_path))
    assert command.argv[command.argv.index("--size") + 1] == "5000"
    with pytest.raises(ValueError, match="only available"):
        ValidateOptions(BuildParser().parse_args([*SelectionArguments('snapshot'), "--baseline-checkout", str(tmp_path)]))
    with pytest.raises(ValueError, match="1..100"):
        ValidateOptions(BuildParser().parse_args([*SelectionArguments('history-write'), "--download-sizes", "5000"]))


def test_removed_composite_identities_cannot_start_unselected_probes() -> None:
    from scripts.checks.catalog import CheckByIdentity
    for identity in ("full", "live", "sources", "runtime", "local"):
        with pytest.raises(ValueError, match="unknown diagnostic identity"):
            CheckByIdentity(identity)


@pytest.mark.parametrize("source", ("composite", "tencent", "eastmoney"))
def test_history_profile_passes_explicit_source_to_the_bounded_probe(source: str) -> None:
    commands = BuildCommands(_options(check_id="history", history_source=source), python_executable="/python")

    assert commands[0].argv[commands[0].argv.index("--source") + 1] == source


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        ("web", "web_health"),
        ("history", "history_sources"),
        ("security-master", "exchange_security_master"),
        ("tencent", "tencent_quotes"),
        ("tencent-transport", "tencent_transport"),
        ("tushare", "tushare_daily"),
        ("history-daily-capability", "history_daily_capability"),
        ("baostock-concurrency", "baostock_concurrency"),
        ("baostock-qfq-shadow", "baostock_qfq_shadow"),
        ("qfq-sqlite", "qfq_sqlite_audit"),
        ("history-sqlite", "history_sqlite_performance"),
        ("history-source-compare", "history_source_compare"),
        ("research-source-compare", "research_source_compare"),
        ("history-space", "history_space"),
        ("history-industry", "history_industry"),
        ("research", "research_readiness"),
        ("long-watchlist", "long_watchlist_admission"),
        ("browser", "browser_refresh"),
        ("browser-live", "browser_live"),
        ("performance", "production_performance:v1,v2,v3,v5"),
    ],
)
def test_single_check_profiles_preserve_targeted_gate_execution(profile: str, expected: str) -> None:
    commands = BuildCommands(_options(check_id=profile), python_executable="/python")

    assert tuple(command.name for command in commands) == (expected,)
    assert commands[0].argv[:2] == ("/python", "-m")


def test_history_space_counts_shared_inodes_once_without_cleanup(tmp_path: Path) -> None:
    month = tmp_path
    published = month / "prices_2026.sqlite3"
    published.write_bytes(b"p" * 4096)
    pending = month / ".prices_2026.pending.sqlite3"
    pending.hardlink_to(published)
    seal = month / ".prices_2026.seal.sqlite3"
    seal.hardlink_to(published)
    (tmp_path / "unsafe").symlink_to(month, target_is_directory=True)
    report = inspect_history_space(tmp_path)
    assert report["groups"] == {
        "pending": {"files": 1, "logical_bytes": 4096},
        "published": {"files": 1, "logical_bytes": 4096},
        "seal": {"files": 1, "logical_bytes": 4096},
    }
    assert report["unique_allocated_bytes"] == published.stat().st_blocks * 512
    assert report["unsafe_symlinks"] == 1
    assert report["cleanup_performed"] is False
    assert pending.exists() and seal.exists()


def test_baostock_concurrency_profile_is_explicitly_non_production() -> None:
    codes = tuple(f"{index:06d}" for index in range(100))
    commands = BuildCommands(_options(check_id="baostock-concurrency", codes=codes), python_executable="/python")

    assert tuple(command.name for command in commands) == ("baostock_concurrency",)
    assert "--sizes" in commands[0].argv
    assert "--codes" in commands[0].argv
    assert commands[0].timeout_seconds >= 1800.0


def test_baostock_concurrency_profile_discovers_universe_for_default_codes() -> None:
    commands = BuildCommands(_options(check_id="baostock-concurrency"), python_executable="/python")

    assert "--discover" in commands[0].argv
    assert "--codes" not in commands[0].argv


def test_serial_rate_experiment_never_starts_parallel_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[float] = []

    def fake_serial(command: baostock_concurrency.WorkerCommand) -> tuple[baostock_concurrency.CodeResult, ...]:
        calls.append(command.interval_seconds)
        return tuple(baostock_concurrency.CodeResult(True, 1, 1, True, 0, 0, 0, None, 1.0) for _ in command.codes)

    monkeypatch.setattr(baostock_concurrency, "_serial", fake_serial)
    report = baostock_concurrency.run_serial_intervals(
        ("600001", "600002"),
        (1, 2),
        days=5,
        timeout_seconds=10.0,
        plan=baostock_concurrency.SerialRatePlan((2.0, 1.5)),
    )

    assert calls == [2.0, 2.0, 1.5, 1.5]
    assert report["parallel_sessions"] == 0
    assert report["production_eligible"] is False


def test_public_serial_experiment_forwards_sizes_intervals_rounds_and_wall_timeout() -> None:
    args = BuildParser().parse_args(
        [
            *SelectionArguments('baostock-concurrency'),
            "--baostock-serial-only",
            "--baostock-intervals",
            "2",
            "1.5",
            "1",
            "--baostock-sizes",
            "10",
            "--baostock-rounds",
            "3",
            "--command-timeout-seconds",
            "400",
            "--history-days",
            "365",
        ]
    )
    options, _ = ValidateOptions(args)
    command = BuildCommands(options)[0]
    assert "--serial-only" in command.argv
    assert command.argv[command.argv.index("--intervals") + 1 :] == ("2.0", "1.5", "1.0")
    assert command.argv[command.argv.index("--rounds") + 1] == "3"
    assert command.argv[command.argv.index("--sizes") + 1] == "10"
    assert command.argv[command.argv.index("--days") + 1] == "365"
    assert command.timeout_seconds == 400
    for bad_args in (["--baostock-rounds", "4"], ["--baostock-sizes", "101"], ["--baostock-intervals", "nan"]):
        with pytest.raises(ValueError):
            ValidateOptions(BuildParser().parse_args([*SelectionArguments('baostock-concurrency'), "--baostock-serial-only", *bad_args]))


def test_real_sdk_fields_list_is_decoded_and_duplicate_dates_are_rejected() -> None:
    class _Rows:
        error_code = "0"
        fields = ["date", "code"]

        def __init__(self, dates: tuple[str, ...]) -> None:
            self.dates = iter(dates)
            self.current = ""

        def next(self) -> bool:
            self.current = next(self.dates, "")
            return bool(self.current)

        def get_row_data(self) -> list[str]:
            return [self.current, "sh.600519"]

    class _Sdk:
        def query_history_k_data_plus(self, *args, **kwargs):
            return _Rows(("2026-10-08", "2026-10-08"))

    assert baostock_concurrency._rows(_Rows(("2026-10-08",))) == [{"date": "2026-10-08", "code": "sh.600519"}]
    result = baostock_concurrency._one(_Sdk(), "600519", days=30)
    assert result.raw_rows == result.qfq_rows == 2
    assert result.ok is False
    assert result.error == "raw_qfq_inconsistent"


def test_serial_probe_reuses_start_spacing_and_stops_after_first_failed_experiment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    starts: list[float] = []

    class _Sdk:
        __version__ = "fixture"

        def query_history_k_data_plus(self, *args, **kwargs):
            starts.append(clock[0])
            clock[0] += 0.75
            return object()

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(baostock_concurrency, "_open", _Sdk)
    monkeypatch.setattr(baostock_concurrency, "_close", lambda sdk: None)
    monkeypatch.setattr(baostock_concurrency, "_rows", lambda result: [{"date": "2026-10-08"}])
    monkeypatch.setattr(
        baostock_concurrency,
        "RateLimitedBaoStockSdk",
        lambda sdk, interval_seconds: RateLimitedBaoStockSdk(
            sdk, interval_seconds=interval_seconds, monotonic=lambda: clock[0], sleep=sleep
        ),
    )
    report = baostock_concurrency.run_serial_intervals(
        ("600519", "000001"),
        (2,),
        days=30,
        timeout_seconds=15,
        plan=baostock_concurrency.SerialRatePlan((1.5,), 3),
    )
    assert report["status"] == "passed"
    assert starts[:4] == [0, 1.5, 3, 4.5]
    assert len(report["experiments"]) == 3
    calls: list[float] = []

    def failed(command):
        calls.append(command.interval_seconds)
        return (baostock_concurrency.CodeResult(False, 0, 0, False, 0, 1, 0, "supplier_timeout", 0),)

    monkeypatch.setattr(baostock_concurrency, "_serial", failed)
    report = baostock_concurrency.run_serial_intervals(
        ("600519",),
        (1,),
        days=30,
        timeout_seconds=15,
        plan=baostock_concurrency.SerialRatePlan((2, 1.5, 1), 3),
    )
    assert report["status"] == "failed"
    assert calls == [2]


def test_public_experiment_report_keeps_metrics_and_drops_vendor_payloads() -> None:
    report = BuildReport(
        "baostock-concurrency",
        (
            DiagnosticResult(
                "baostock_concurrency",
                0,
                10,
                {
                    "status": "passed",
                    "parallel_sessions": 0,
                    "rate_semantics": "query_start_to_start",
                    "experiments": [
                        {"sample_size": 10, "success_count": 10, "interval_seconds": 1.5, "raw_payload": "secret"}
                    ],
                },
                None,
            ),
        ),
    )
    check = report["checks"][0]
    assert check["experiments"][0]["interval_seconds"] == 1.5
    assert check["experiments"][0]["success_count"] == 10
    assert "raw_payload" not in check["experiments"][0]
    assert check["production_eligible"] is False


def test_serial_failure_stops_stock_requests_and_counts_login_timeout_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(baostock_concurrency, "_open", lambda: object())
    monkeypatch.setattr(baostock_concurrency, "_close", lambda sdk: None)
    monkeypatch.setattr(baostock_concurrency, "RateLimitedBaoStockSdk", lambda sdk, **kwargs: sdk)

    def fail(sdk, code, *, days):
        calls.append(code)
        return baostock_concurrency.CodeResult(False, 0, 0, False, 1, 0, 0, "network_error", 1)

    monkeypatch.setattr(baostock_concurrency, "_one", fail)
    command = baostock_concurrency.WorkerCommand(("600519", "000001", "300750"), 400, 15, 2)
    results = baostock_concurrency._serial(command)
    assert calls == ["600519"]
    assert [item.error for item in results] == ["network_error", "supplier_batch_stopped", "supplier_batch_stopped"]
    assert sum(item.network_errors for item in results) == 1

    def login_timeout():
        raise TimeoutError("timeout")

    monkeypatch.setattr(baostock_concurrency, "_open", login_timeout)
    results = baostock_concurrency._serial(command)
    assert len(results) == 3
    assert sum(item.timeouts for item in results) == 1


def test_missing_sample_results_cannot_pass_rate_experiment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(baostock_concurrency, "_serial", lambda command: ())
    report = baostock_concurrency.run_serial_intervals(
        ("600519",), (1,), days=400, timeout_seconds=15, plan=baostock_concurrency.SerialRatePlan((2, 1.5), 3)
    )
    assert report["status"] == "failed"
    assert len(report["experiments"]) == 1
    assert report["experiments"][0]["failure_count"] == 1
    assert report["experiments"][0]["error_categories"] == ["sample_count_mismatch"]


@pytest.mark.parametrize("invalid_date", ["", "2026-13-01", "2099-01-01"])
def test_invalid_paired_dates_do_not_pass_rate_experiment(monkeypatch: pytest.MonkeyPatch, invalid_date: str) -> None:
    monkeypatch.setattr(baostock_concurrency, "_query_one", lambda *args: [{"date": invalid_date}])
    assert baostock_concurrency._one(object(), "600519", days=400).ok is False


def test_baostock_qfq_shadow_profile_is_read_only_and_uses_configured_history_root() -> None:
    commands = BuildCommands(_options(check_id="baostock-qfq-shadow"), python_executable="/python")

    assert tuple(command.name for command in commands) == ("baostock_qfq_shadow",)
    assert commands[0].argv[:3] == ("/python", "-m", "scripts.diagnose_runtime")
    assert commands[0].argv[-2:] == ("--history-root", "data/history")


def test_research_profile_runs_only_research_readiness_probe() -> None:
    commands = BuildCommands(_options(check_id="research"), python_executable="/python")

    assert tuple(command.name for command in commands) == ("research_readiness",)
    assert commands[0].argv == (
        "/python",
        "-m",
        "trader.entrypoints.cli",
        "--config",
        "config/runtime.json",
        "research-status",
    )


def test_research_source_compare_projects_bounded_component_errors() -> None:
    report = BuildReport(
        "research-source-compare",
        (
            DiagnosticResult(
                "research_source_compare",
                0,
                4.0,
                {
                    "schema_version": "research-source-comparison",
                    "status": "degraded",
                    "required_fields": ["financial", "pledge"],
                    "sources": [
                        {
                            "source": "eastmoney",
                            "tested": True,
                            "excluded_by_budget": False,
                            "physical_http_requests": 5,
                            "sample_count": 1,
                            "field_coverage": {"financial": {"covered": 1, "sample_count": 1}},
                            "interface_status": "partial",
                            "samples": [
                                {
                                    "code": "600519",
                                    "physical_http_requests": 5,
                                    "missing_fields": ["pledge"],
                                    "component_errors": ["pledge:Timeout"],
                                    "raw_payload": "secret",
                                }
                            ],
                            "raw_payload": "secret",
                        },
                        {
                            "source": "cninfo",
                            "tested": True,
                            "excluded_by_budget": False,
                            "physical_http_requests": 2,
                            "sample_count": 1,
                            "interface_status": "complete",
                            "production_role": "issuer_announcement_history_supplement",
                            "regulatory_history_complete": False,
                            "exchange_cross_check_status": "pending",
                        },
                    ],
                    "selected_route": {"primary": None},
                    "budget": {"elapsed_ms": 4.0},
                },
                None,
            ),
        ),
    )

    summary = report["checks"][0]["summary"]
    assert summary["required_fields"] == ["financial", "pledge"]
    assert summary["sources"][0]["missing_field_counts"] == {"pledge": 1}
    assert summary["sources"][0]["component_error_counts"] == {"pledge:Timeout": 1}
    assert summary["sources"][1]["production_role"] == "issuer_announcement_history_supplement"
    assert summary["sources"][1]["regulatory_history_complete"] is False
    assert summary["sources"][1]["exchange_cross_check_status"] == "pending"
    assert "600519" not in str(summary)
    assert "raw_payload" not in str(summary)


def test_research_status_projects_target_selection_separately_from_previous_research() -> None:
    report = BuildReport(
        "research",
        (
            DiagnosticResult(
                "research_readiness",
                0,
                4.0,
                {
                    "schema_version": "research_readiness",
                    "history_archive": {
                        "state": "invalid",
                        "snapshot_hash": "a" * 64,
                        "calendar_sessions": 2000,
                        "universe_count": 5453,
                        "partition_count": 100,
                        "data_cutoff": "2026-09-10",
                        "label_cutoff": "2026-09-09",
                        "reason": "history_snapshot_partition_invalid",
                        "production_authority": False,
                        "point_in_time_parity": False,
                    },
                    "tomorrow_research": {
                        "status": "blocked",
                        "next_stage": "resource_probe",
                        "input_prerequisite_status": "blocked",
                        "input_blockers": ["tomorrow_h1_historical_data_insufficient"],
                        "production_blockers": ["daily_close_proxy_not_validated"],
                        "production_readiness": "production_adaptation_blocked",
                        "production_authority": False,
                    },
                    "production_authority": False,
                    "recorded_trade_dates": ["2026-08-21", "2026-08-20"],
                    "blockers": ["target_selection_external_validation_required"],
                    "target_selection": {
                        "research_identity": "eleven_layer_full_population",
                        "status": "external_validation_required",
                        "strategies": ["tomorrow", "d25"],
                        "population": "full_eligible_population",
                        "validation_command": "research-selection",
                        "blockers": ["target_selection_external_validation_required"],
                        "production_authority": False,
                    },
                },
                None,
            ),
        ),
    )

    assert report["status"] == "failed"
    summary = report["checks"][0]["summary"]
    assert summary["history_archive"] == {
        "state": "invalid",
        "snapshot_hash": "a" * 64,
        "calendar_sessions": 2000,
        "universe_count": 5453,
        "partition_count": 100,
        "data_cutoff": "2026-09-10",
        "label_cutoff": "2026-09-09",
        "reason": "history_snapshot_partition_invalid",
        "production_authority": False,
        "point_in_time_parity": False,
    }
    assert summary["v3"] == {
        "status": "blocked",
        "next_stage": "resource_probe",
        "input_prerequisite_status": "blocked",
        "input_blockers": ["tomorrow_h1_historical_data_insufficient"],
        "production_blockers": ["daily_close_proxy_not_validated"],
        "production_readiness": "production_adaptation_blocked",
        "production_authority": False,
    }
    assert summary["production_authority"] is False
    assert summary["target_selection"]["research_identity"] == "eleven_layer_full_population"
    assert summary["target_selection"]["strategies"] == ["tomorrow", "d25"]
    assert report["findings"][0]["code"] == "target_selection_external_validation_required"
    assert "2026-08-20" not in str(report["checks"][0])


def test_research_status_projection_rejects_unsupported_schema() -> None:
    report = BuildReport(
        "research",
        (
            DiagnosticResult(
                "research_readiness",
                0,
                1.0,
                {"schema_version": "research_readiness_unsupported", "research_state": "historical_collecting"},
                None,
            ),
        ),
    )

    assert report["status"] == "failed"
    assert report["findings"][0]["code"] == "research_status_shape_invalid"


def test_model_steady_only_routes_to_existing_performance_owner():
    options, _ = ValidateOptions(BuildParser().parse_args([*SelectionArguments('performance'), "--performance-steady-only"]))
    command = BuildCommands(options, python_executable="python3")[0]
    assert "--steady-only" in command.argv
    for args in ([*SelectionArguments('web'), "--performance-steady-only"],
                 [*SelectionArguments('performance'), "--performance-steady-only", "--performance-replay-ticks", "1"]):
        with pytest.raises(ValueError, match="steady-only"):
            ValidateOptions(BuildParser().parse_args(args))


def test_combined_report_is_bounded_and_does_not_forward_prices_or_vendor_payloads() -> None:
    results = (
        DiagnosticResult(
            "web_health",
            0,
            12.5,
            {
                "schema_version": "web_recommendation_health",
                "status": "passed",
                "summary": {"error_count": 0, "warning_count": 0},
                "findings": [],
                "samples": [
                    {
                        "degraded_reasons": ["observer:ResearchTraceCapacityError"],
                        "recommendation_round": {"sequence": 17, "profiles": ["v1", "v5"],
                                                 "statements": 0, "worker_inflight": 0,
                                                 "sql": "must-not-escape"},
                        "scoring_profile": {
                            "profile_id": "v2",
                            "heads": {
                                "tomorrow": {
                                    "profile_id": "v2",
                                    "is_enable": True,
                                    "model_id": "industry_ridge_lightgbm",
                                    "model_hash": "a" * 64,
                                    "request_count": 3,
                                    "candidate_count": 0,
                                    "predictor_batch_count": 0,
                                    "cache_hit_count": 2,
                                    "feature_payload": "must-not-escape",
                                }
                            },
                        },
                        "market": {
                            "candidate_quote_age": {
                                "p50_seconds": 1.0,
                                "p95_seconds": 2.0,
                                "maximum_seconds": 3.0,
                                "sample_count": 360,
                            },
                            "history_archive": {"maintenance_completed_units": 20},
                        },
                        "company_research": {
                            "state": "idle",
                            "running_codes": 0,
                            "pending_codes": 0,
                            "completed_batches": 2,
                            "tracked_output_codes": 12,
                        },
                        "recent_errors": [
                            {
                                "code": "refresh:timeout",
                                "stage": "refresh",
                                "severity": "degraded",
                                "count": 7,
                                "recovery_status": "recovered",
                                "private": "must-not-escape",
                            }
                        ],
                    }
                ],
            },
            None,
        ),
        DiagnosticResult(
            "history_sources",
            0,
            20.0,
            {
                "schema_version": "history-source-sampling",
                "status": "degraded",
                "summary": {"usable_observations": 2, "error_observations": 1},
                "observations": [{"code": "600519", "error": "secret vendor payload"}],
            },
            None,
        ),
        DiagnosticResult(
            "tencent_quotes",
            0,
            4.0,
            {
                "schema_version": "tencent-quote-sampling",
                "latency": {"p95_ms": 4.0},
                "source_changed": True,
                "samples": [{"quotes": [{"code": "600519", "price": 999.0}]}],
            },
            None,
        ),
    )

    report = BuildReport("web", results)
    rendered = str(report)

    assert report["status"] == "degraded"
    assert report["summary"] == {"passed": 2, "degraded": 1, "failed": 0, "total": 3, "timed_out": 0, "cancelled": 0, "skipped": 0, "empty": 0}
    assert "600519" not in rendered
    assert "999.0" not in rendered
    assert "secret vendor payload" not in rendered
    assert "must-not-escape" not in rendered
    assert report["checks"][0]["latest_runtime"]["recommendation_round"]["sequence"] == 17
    assert report["checks"][0]["latest_runtime"]["candidate_quote_age"]["p95_seconds"] == 2.0
    issue = report["checks"][0]["latest_runtime"]["recent_errors"][0]
    assert issue["count"] == 7 and issue["recovery_status"] == "recovered"
    assert report["checks"][0]["latest_runtime"]["history_archive"]["maintenance_completed_units"] == 20
    assert report["checks"][0]["latest_runtime"]["company_research"] == {
        "state": "idle",
        "running_codes": 0,
        "pending_codes": 0,
        "completed_batches": 2,
        "tracked_output_codes": 12,
    }
    assert report["checks"][0]["latest_runtime"]["degraded_reasons"] == ["observer:ResearchTraceCapacityError"]
    assert report["checks"][0]["latest_runtime"]["scoring_profile"] == {
        "profile_id": "v2",
        "heads": {
            "tomorrow": {
                "profile_id": "v2",
                "is_enable": True,
                "model_id": "industry_ridge_lightgbm",
                "model_hash": "a" * 64,
                "request_count": 3,
                "candidate_count": 0,
                "predictor_batch_count": 0,
                "cache_hit_count": 2,
                "score_scale": None,
                "production_authority": None,
                "training_label_cutoff": None,
            }
        },
    }


def test_daily_history_capability_projection_keeps_only_the_audit_whitelist() -> None:
    report = BuildReport(
        "history-daily-capability",
        (
            DiagnosticResult(
                "history_daily_capability",
                0,
                8.0,
                {
                    "schema_version": "history-daily-capability-audit",
                    "status": "degraded",
                    "freeze": {"status": "blocked", "efficient_daily_source": None},
                    "baostock_lower_bound": {"minimum_raw_qfq_calls": 10906},
                    "candidates": [
                        {
                            "source": "baostock",
                            "request_scope": "security_range",
                            "market_day_batch": False,
                            "probe_status": "failed",
                            "probe_error": "supplier_call_timeout",
                            "vendor_payload": "must-not-escape",
                        }
                    ],
                    "configuration": {"runtime_config": "/private/path"},
                },
                None,
            ),
        ),
    )

    rendered = str(report)
    assert report["status"] == "degraded"
    assert report["checks"][0]["summary"]["candidates"][0] == {
        "source": "baostock",
        "request_scope": "security_range",
        "market_day_batch": False,
        "market_day_raw": None,
        "adjustment_factor": None,
        "raw_qfq_semantics_observed": None,
        "probe_status": "failed",
        "probe_error": "supplier_call_timeout",
    }
    assert "must-not-escape" not in rendered
    assert "/private/path" not in rendered


def test_runner_continues_after_a_failed_check_and_preserves_check_order() -> None:
    commands = tuple(BuildCommands(_options(check_id=identity), python_executable="/python")[0] for identity in ("security-master", "history", "tencent", "tushare", "history-daily-capability"))
    called: list[str] = []

    def runner(command):
        called.append(command.name)
        if command.name == "history_sources":
            return DiagnosticResult(command.name, 1, 1.0, None, "invalid_json")
        return DiagnosticResult(
            command.name,
            0,
            1.0,
            {"schema_version": f"{command.name}-diagnostic", "status": "passed"},
            None,
        )

    report = RunDiagnostics("history", commands, runner=runner)

    assert called == [
        "exchange_security_master",
        "history_sources",
        "tencent_quotes",
        "tushare_daily",
        "history_daily_capability",
    ]
    assert report["status"] == "failed"
    assert report["summary"]["failed"] == 1


def test_child_launch_failure_becomes_a_result_instead_of_aborting_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_launch(*args: object, **kwargs: object) -> None:
        raise OSError("diagnostic executable is unavailable")

    monkeypatch.setattr("scripts.checks.execution.RunDatabaseProbe", fail_launch)

    result = ExecuteCommand(DiagnosticCommand("web_health", ("missing",), 1.0))

    assert result.return_code == 126
    assert result.error_code == "command_launch_failed"
    assert result.payload is None


def test_qfq_sqlite_profile_audits_the_configured_root_read_only():
    options, _ = ValidateOptions(BuildParser().parse_args([*SelectionArguments('qfq-sqlite')]))
    command = BuildCommands(options, python_executable="python3")[0]
    assert command.name == "qfq_sqlite_audit"
    assert "scripts.checks.download.history.qfq_sqlite" in command.argv
    assert "--database" in command.argv
    assert "--rounds" not in command.argv

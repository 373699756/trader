from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from scripts.diagnose_runtime import (
    DiagnosticCommand,
    DiagnosticOptions,
    DiagnosticResult,
    _parser,
    _validate,
    build_commands,
    build_report,
    execute_command,
    run_diagnostics,
)
from scripts.runtime_diagnostics import baostock_concurrency
from scripts.runtime_diagnostics.browser_refresh import _seed
from trader.download.infra.baostock_session import RateLimitedBaoStockSdk
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import UnifiedDecisionIndex
from trader.recommendation.application.runtime.schedule import SHANGHAI
from trader.recommendation.domain.publication.models import Strategy


def _options(**overrides: object) -> DiagnosticOptions:
    defaults = DiagnosticOptions(
        profile="live",
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
        history_root=Path("data/history/baostock"),
        sqlite_page_sample_count=1,
        sqlite_query_rounds=3,
        sqlite_revision_write_sample_count=512,
    )
    return replace(defaults, **overrides)


def test_browser_diagnostic_seed_matches_the_current_decision_item_contract() -> None:
    index = UnifiedDecisionIndex()
    observed_at = datetime(2026, 8, 24, 13, 5, tzinfo=SHANGHAI)

    _seed(index, Strategy.TOMORROW, observed_at, "600002")

    current = index.snapshot(Strategy.TOMORROW).current
    assert current is not None
    assert current.items[0].board.value == "main"
    assert current.items[0].selection_rank == 1


def test_live_profile_combines_runtime_and_all_source_probes() -> None:
    commands = build_commands(_options(), python_executable="/python")

    assert tuple(command.name for command in commands) == (
        "web_health",
        "exchange_security_master",
        "history_sources",
        "tencent_quotes",
        "tushare_daily",
        "history_daily_capability",
    )
    assert all(command.argv[0] == "/python" for command in commands)
    assert all("--output" not in command.argv for command in commands)


def test_full_profile_adds_browser_and_offline_performance_without_duplicate_probes() -> None:
    commands = build_commands(_options(profile="full"), python_executable="/python")

    assert tuple(command.name for command in commands) == (
        "web_health",
        "exchange_security_master",
        "history_sources",
        "tencent_quotes",
        "tushare_daily",
        "history_daily_capability",
        "browser_refresh",
        "production_performance",
    )


@pytest.mark.parametrize("source", ("composite", "tencent", "eastmoney"))
def test_history_profile_passes_explicit_source_to_the_bounded_probe(source: str) -> None:
    commands = build_commands(_options(profile="history", history_source=source), python_executable="/python")

    assert commands[0].argv[commands[0].argv.index("--source") + 1] == source


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        ("web", "web_health"),
        ("history", "history_sources"),
        ("security-master", "exchange_security_master"),
        ("tencent", "tencent_quotes"),
        ("tushare", "tushare_daily"),
        ("history-daily-capability", "history_daily_capability"),
        ("baostock-concurrency", "baostock_concurrency"),
        ("baostock-qfq-shadow", "baostock_qfq_shadow"),
        ("history-sqlite", "history_sqlite_performance"),
        ("research", "research_readiness"),
        ("long-watchlist", "long_watchlist_admission"),
        ("browser", "browser_refresh"),
        ("performance", "production_performance"),
    ],
)
def test_single_check_profiles_preserve_targeted_gate_execution(profile: str, expected: str) -> None:
    commands = build_commands(_options(profile=profile), python_executable="/python")

    assert tuple(command.name for command in commands) == (expected,)
    assert commands[0].argv[:2] == ("/python", "-m")


def test_baostock_concurrency_profile_is_explicitly_non_production() -> None:
    codes = tuple(f"{index:06d}" for index in range(100))
    commands = build_commands(_options(profile="baostock-concurrency", codes=codes), python_executable="/python")

    assert tuple(command.name for command in commands) == ("baostock_concurrency",)
    assert "--sizes" in commands[0].argv
    assert "--codes" in commands[0].argv
    assert commands[0].timeout_seconds >= 1800.0


def test_baostock_concurrency_profile_discovers_universe_for_default_codes() -> None:
    commands = build_commands(_options(profile="baostock-concurrency"), python_executable="/python")

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
    args = _parser().parse_args(
        [
            "--profile",
            "baostock-concurrency",
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
    options, _ = _validate(args)
    command = build_commands(options)[0]
    assert "--serial-only" in command.argv
    assert command.argv[command.argv.index("--intervals") + 1 :] == ("2.0", "1.5", "1.0")
    assert command.argv[command.argv.index("--rounds") + 1] == "3"
    assert command.argv[command.argv.index("--sizes") + 1] == "10"
    assert command.argv[command.argv.index("--days") + 1] == "365"
    assert command.timeout_seconds == 400
    for bad_args in (["--baostock-rounds", "4"], ["--baostock-sizes", "101"], ["--baostock-intervals", "nan"]):
        with pytest.raises(ValueError):
            _validate(_parser().parse_args(["--profile", "baostock-concurrency", "--baostock-serial-only", *bad_args]))


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
    report = build_report(
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


def test_baostock_qfq_shadow_profile_is_read_only_and_uses_active_history_root() -> None:
    commands = build_commands(_options(profile="baostock-qfq-shadow"), python_executable="/python")

    assert tuple(command.name for command in commands) == ("baostock_qfq_shadow",)
    assert commands[0].argv[:3] == ("/python", "-m", "scripts.runtime_diagnostics.baostock_qfq_shadow")
    assert commands[0].argv[-2:] == ("--history-root", "data/history/baostock")


def test_research_profile_runs_only_research_readiness_probe() -> None:
    commands = build_commands(_options(profile="research"), python_executable="/python")

    assert tuple(command.name for command in commands) == ("research_readiness",)
    assert commands[0].argv == (
        "/python",
        "-m",
        "trader.entrypoints.cli",
        "--config",
        "config/runtime.json",
        "research-status",
    )


def test_research_status_projection_uses_the_active_monthly_archive_and_v3_blockers() -> None:
    report = build_report(
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
                        "active_snapshot_hash": "a" * 64,
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
                },
                None,
            ),
        ),
    )

    assert report["status"] == "failed"
    summary = report["checks"][0]["summary"]
    assert summary["history_archive"] == {
        "state": "invalid",
        "active_snapshot_hash": "a" * 64,
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
    assert report["findings"][0]["code"] == "tomorrow_h1_historical_data_insufficient"
    assert "2026-08-20" not in str(report["checks"][0])


def test_research_status_projection_rejects_unsupported_schema() -> None:
    report = build_report(
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
                        "scoring_profile": {
                            "profile_id": "v2",
                            "heads": {
                                "tomorrow": {
                                    "profile_id": "v2",
                                    "active": True,
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

    report = build_report("live", results)
    rendered = str(report)

    assert report["status"] == "degraded"
    assert report["summary"] == {"passed": 2, "degraded": 1, "failed": 0, "total": 3}
    assert "600519" not in rendered
    assert "999.0" not in rendered
    assert "secret vendor payload" not in rendered
    assert "must-not-escape" not in rendered
    assert report["checks"][0]["latest_runtime"]["candidate_quote_age"]["p95_seconds"] == 2.0
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
                "active": True,
                "model_id": "industry_ridge_lightgbm",
                "model_hash": "a" * 64,
                "request_count": 3,
                "candidate_count": 0,
                "predictor_batch_count": 0,
                "cache_hit_count": 2,
            }
        },
    }


def test_daily_history_capability_projection_keeps_only_the_audit_whitelist() -> None:
    report = build_report(
        "history-daily-capability",
        (
            DiagnosticResult(
                "history_daily_capability",
                0,
                8.0,
                {
                    "schema_version": "history-daily-capability-audit",
                    "status": "degraded",
                    "decision": {"status": "blocked", "efficient_daily_source": None},
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
    commands = build_commands(_options(profile="sources"), python_executable="/python")
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

    report = run_diagnostics("sources", commands, runner=runner)

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

    monkeypatch.setattr("scripts.diagnose_runtime.subprocess.run", fail_launch)

    result = execute_command(DiagnosticCommand("web_health", ("missing",), 1.0))

    assert result.return_code == 126
    assert result.error_code == "command_launch_failed"
    assert result.payload is None

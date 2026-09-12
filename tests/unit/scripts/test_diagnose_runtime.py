from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from scripts.diagnose_runtime import (
    DiagnosticCommand,
    DiagnosticOptions,
    DiagnosticResult,
    build_commands,
    build_report,
    execute_command,
    run_diagnostics,
)
from scripts.runtime_diagnostics.browser_refresh import _seed
from trader.application.decisions.decision_core import UnifiedDecisionIndex
from trader.application.runtime.schedule import SHANGHAI
from trader.domain.recommendation.models import Strategy


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
        persistence_runtime_dir=None,
        archive_root=Path("data/history/baostock"),
        archive_page_sample_count=1,
        archive_query_rounds=3,
        archive_revision_write_sample_count=512,
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
        ("history-archive", "history_archive_performance"),
        ("research", "research_readiness"),
        ("browser", "browser_refresh"),
        ("performance", "production_performance"),
    ],
)
def test_single_check_profiles_preserve_targeted_gate_execution(profile: str, expected: str) -> None:
    commands = build_commands(_options(profile=profile), python_executable="/python")

    assert tuple(command.name for command in commands) == (expected,)
    assert commands[0].argv[:2] == ("/python", "-m")


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
                        "market": {
                            "candidate_quote_age": {
                                "p50_seconds": 1.0,
                                "p95_seconds": 2.0,
                                "maximum_seconds": 3.0,
                                "sample_count": 360,
                            },
                            "history_warmup": {"completed_count": 20},
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
    assert report["checks"][0]["latest_runtime"]["candidate_quote_age"]["p95_seconds"] == 2.0
    assert report["checks"][0]["latest_runtime"]["history_warmup"]["completed_count"] == 20
    assert report["checks"][0]["latest_runtime"]["company_research"] == {
        "state": "idle",
        "running_codes": 0,
        "pending_codes": 0,
        "completed_batches": 2,
        "tracked_output_codes": 12,
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

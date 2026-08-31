from __future__ import annotations

from dataclasses import replace
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


def _options(**overrides: object) -> DiagnosticOptions:
    defaults = DiagnosticOptions(
        profile="live",
        base_url="http://127.0.0.1:5000",
        runtime_config=Path("config/v2/runtime.json"),
        codes=("600519", "300750", "688981"),
        web_samples=3,
        web_interval_seconds=2.0,
        source_samples=1,
        source_interval_seconds=1.0,
        history_workers=3,
        history_days=61,
        history_source="composite",
        web_timeout_seconds=3.0,
        source_timeout_seconds=4.5,
        browser_duration_seconds=8.0,
        browser_minimum_updates=3,
        command_timeout_seconds=180.0,
        persistence_runtime_dir=None,
    )
    return replace(defaults, **overrides)


def test_live_profile_combines_runtime_and_all_source_probes() -> None:
    commands = build_commands(_options(), python_executable="/python")

    assert tuple(command.name for command in commands) == (
        "web_health",
        "exchange_security_master",
        "history_sources",
        "tencent_quotes",
        "tushare_daily",
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
        ("research", "score_p0_readiness"),
        ("browser", "browser_refresh"),
        ("performance", "production_performance"),
    ],
)
def test_single_check_profiles_preserve_targeted_gate_execution(profile: str, expected: str) -> None:
    commands = build_commands(_options(profile=profile), python_executable="/python")

    assert tuple(command.name for command in commands) == (expected,)
    assert commands[0].argv[:2] == ("/python", "-m")


def test_research_profile_runs_only_score_p0_readiness_probe() -> None:
    commands = build_commands(_options(profile="research"), python_executable="/python")

    assert tuple(command.name for command in commands) == ("score_p0_readiness",)
    assert commands[0].argv == (
        "/python",
        "-m",
        "trader.entrypoints.cli",
        "--config",
        "config/v2/runtime.json",
        "research-status",
    )


def test_research_status_projection_uses_authoritative_active_window_and_reports_failure() -> None:
    report = build_report(
        "research",
        (
            DiagnosticResult(
                "score_p0_readiness",
                0,
                4.0,
                {
                    "schema_version": "v2_research_readiness_v5",
                    "research_state": "historical_collection_failed",
                    "recorded_trade_dates": ["2026-08-21", "2026-08-20"],
                    "active_research": {
                        "research_identity": "score_p0_v2",
                        "evaluation_blocker": "score_p0_v2_historical_planned_dates_missed",
                        "historical_window": {
                            "recorded_trade_dates": 1,
                            "maximum_attainable_trade_dates": 36,
                            "recoverable": False,
                        },
                        "forward_window": {
                            "recorded_trade_dates": 0,
                            "maximum_attainable_trade_dates": 20,
                            "recoverable": True,
                        },
                    },
                },
                None,
            ),
        ),
    )

    assert report["status"] == "failed"
    assert report["checks"][0]["summary"] == {
        "research_identity": "score_p0_v2",
        "historical_window": {
            "recorded_trade_dates": 1,
            "maximum_attainable_trade_dates": 36,
            "recoverable": False,
        },
        "forward_window": {
            "recorded_trade_dates": 0,
            "maximum_attainable_trade_dates": 20,
            "recoverable": True,
        },
        "recorded_count": 1,
        "status": "historical_collection_failed",
        "evaluation_blocker": "score_p0_v2_historical_planned_dates_missed",
    }
    assert report["findings"][0]["code"] == "score_p0_v2_historical_planned_dates_missed"
    assert "2026-08-20" not in str(report["checks"][0])


def test_research_status_projection_rejects_unsupported_schema() -> None:
    report = build_report(
        "research",
        (
            DiagnosticResult(
                "score_p0_readiness",
                0,
                1.0,
                {"schema_version": "v2_research_readiness_v2", "research_state": "historical_collecting"},
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
                "schema_version": "web_recommendation_health_v3",
                "status": "passed",
                "summary": {"error_count": 0, "warning_count": 0},
                "findings": [],
                "samples": [{"market": {"history_warmup": {"completed_count": 20}}}],
            },
            None,
        ),
        DiagnosticResult(
            "history_sources",
            0,
            20.0,
            {
                "schema_version": "history-source-sampling-v1",
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
                "schema_version": "tencent-quote-sampling-v1",
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
    assert report["checks"][0]["latest_runtime"]["history_warmup"]["completed_count"] == 20


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
            {"schema_version": f"{command.name}-v1", "status": "passed"},
            None,
        )

    report = run_diagnostics("sources", commands, runner=runner)

    assert called == ["exchange_security_master", "history_sources", "tencent_quotes", "tushare_daily"]
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

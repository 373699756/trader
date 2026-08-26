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
        "history_sources",
        "tencent_quotes",
        "tushare_daily",
    )
    assert all(command.argv[0] == "/python" for command in commands)
    assert all("--output" in command.argv and command.argv[-1] == "-" for command in commands)


def test_full_profile_adds_browser_and_offline_performance_without_duplicate_probes() -> None:
    commands = build_commands(_options(profile="full"), python_executable="/python")

    assert tuple(command.name for command in commands) == (
        "web_health",
        "history_sources",
        "tencent_quotes",
        "tushare_daily",
        "browser_refresh",
        "production_performance",
    )


def test_combined_report_is_bounded_and_does_not_forward_prices_or_vendor_payloads() -> None:
    results = (
        DiagnosticResult(
            "web_health",
            0,
            12.5,
            {
                "schema_version": "web_recommendation_health_v1",
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

    assert called == ["history_sources", "tencent_quotes", "tushare_daily"]
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

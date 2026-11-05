"""Explicit foreground measurement of the existing company sync against current data."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import redirect_stdout
from pathlib import Path

from scripts.core.contracts import ToolOutcome
from scripts.core.paths import ExternalPath
from scripts.core.projection_values import ProjectMapping

_MAX_REPORT_BYTES = 1024 * 1024
_SCHEMA = "company-live-sync-measurement"
_MEASUREMENT_FIELDS = (
    "schema_version", "cancelled", "writes_current_company_data", "isolated_sample", "status_basis",
    "field_correctness_verified", "fill_missing", "runtime_configuration_sha256", "cli_exit_code",
    "failure_category", "wall_seconds", "process_cpu_seconds", "operation_timing", "operation_counts",
    "route_classification", "thread_cpu_includes_child_processes", "coverage", "snapshot_interval_seconds",
    "snapshot_trigger", "snapshot_failed", "physical_io_includes_running_reports", "database_statistics_final",
)
_DATABASE_FIELDS = (
    "wall_seconds", "cpu_seconds", "connections", "statements", "transactions", "commits",
    "sqlite_call_seconds", "commit_seconds", "physical_read_bytes", "physical_write_bytes",
    "sampled_wal_peak_bytes", "wal_sampling_enabled", "wal_sampling_failed", "process_peak_rss_bytes", "completed",
)


def _read_measurement(path: Path) -> dict[str, object]:
    if path.is_symlink():
        raise ValueError("company_live_sync_measurement_invalid")
    with path.open("rb") as stream:
        content = stream.read(_MAX_REPORT_BYTES + 1)
    if len(content) > _MAX_REPORT_BYTES:
        raise ValueError("company_live_sync_measurement_too_large")
    raw = json.loads(content)
    if not isinstance(raw, dict) or raw.get("schema_version") != _SCHEMA:
        raise ValueError("company_live_sync_measurement_invalid")
    if (
        raw.get("field_correctness_verified") is not False
        or raw.get("writes_current_company_data") is not True
        or raw.get("isolated_sample") is not False
        or raw.get("database_statistics_final") is not True
        or raw.get("status") not in ("passed", "degraded", "failed")
        or not isinstance(raw.get("cancelled"), bool)
        or raw.get("cli_exit_code") is not None and type(raw["cli_exit_code"]) is not int
    ):
        raise ValueError("company_live_sync_measurement_invalid")
    payload = {name: raw[name] for name in _MEASUREMENT_FIELDS if name in raw}
    payload["measurement_status"] = raw["status"]
    database = ProjectMapping(raw.get("database"))
    payload["database"] = {name: database[name] for name in _DATABASE_FIELDS if name in database}
    operations = raw.get("operations")
    payload["operations"] = [
        {name: row.get(name) for name in ("operation", "calls", "wall_seconds", "thread_cpu_seconds")} | {
            "failures": [
                {name: failure.get(name) for name in ("category", "calls")}
                for failure in (row.get("failures", [])[:32] if isinstance(row.get("failures"), list) else [])
                if isinstance(failure, dict)
            ]
        }
        for row in (operations[:64] if isinstance(operations, list) else [])
        if isinstance(row, dict)
    ]
    return payload


def Run(action_identity: str, arguments: list[str]) -> ToolOutcome:
    """Require an explicit new measurement artifact; preserve CLI failure and cancellation."""
    if action_identity != "company-live-sync":
        raise ValueError("unknown_company_live_sync_action")
    parser = argparse.ArgumentParser(
        description="Measure the real company sync; this explicitly WRITES CURRENT COMPANY DATA.",
        allow_abbrev=False,
    )
    parser.add_argument("--runtime-config", type=Path, required=True)
    parser.add_argument("--measurement-output", type=Path, required=True, help="new external absolute measurement JSON file")
    parser.add_argument("--fill", action="store_true", help="explicitly pass --fill to the existing company sync")
    args = parser.parse_args(arguments)
    try:
        destination = ExternalPath(args.measurement_output, "--measurement-output")
        if destination is None or args.measurement_output.is_symlink() or destination.exists():
            raise ValueError("measurement output must be a new external absolute file")
        configuration = args.runtime_config.expanduser().resolve(strict=True)
    except (OSError, ValueError):
        parser.error("live sync requires an existing configuration and a new external absolute measurement file")
    from scripts.download.company.company_pipeline_audit import MeasureCompanySync

    code = 1
    try:
        with redirect_stdout(sys.stderr):
            code = MeasureCompanySync(configuration, destination, fill_missing=args.fill)
    except KeyboardInterrupt:
        code = 130
    except SystemExit as exc:
        code = exc.code if type(exc.code) is int and exc.code != 0 else 1
    except (OSError, RuntimeError, ValueError, TypeError, sqlite3.Error):
        code = 1
    try:
        payload = _read_measurement(destination)
    except (OSError, ValueError, UnicodeError):
        return ToolOutcome(code if code != 0 else 1, {
            "schema_version": _SCHEMA,
            "status": "cancelled" if code == 130 else "failed",
            "cli_exit_code": None,
            "field_correctness_verified": False,
            "measurement_available": False,
            "error_code": "company_live_sync_measurement_unavailable",
        })
    payload["measurement_available"] = True
    passed = code == 0 and payload.get("cli_exit_code") == 0 and payload["measurement_status"] == "passed" and payload.get("cancelled") is False
    payload["status"] = "passed" if passed else "cancelled" if code == 130 else "failed"
    if not passed and code == 0:
        code = 1
        payload["error_code"] = "company_live_sync_measurement_not_completed"
    return ToolOutcome(code, payload)

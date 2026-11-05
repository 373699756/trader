"""Generic result classification and selected-owner report composition."""

from __future__ import annotations

import importlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from typing import Protocol, cast
from zoneinfo import ZoneInfo

from scripts.core.catalog import ToolByIdentity
from scripts.core.contracts import CheckStatus, DiagnosticResult
from scripts.core.projection_values import ProjectMapping

_SHANGHAI = ZoneInfo("Asia/Shanghai")


class _Reporter(Protocol):
    def NormalizeResult(self, result: DiagnosticResult, *, local: bool = False) -> DiagnosticResult: ...

    def ProjectDetails(self, result: DiagnosticResult, source: Mapping[str, object], payload: dict[str, object]) -> None: ...

    def CompleteReport(self, report: dict[str, object], results: Sequence[DiagnosticResult]) -> None: ...


class _TextReporter(Protocol):
    def RenderText(self, report: Mapping[str, object]) -> str: ...


def _reporter(action_identity: str) -> _Reporter | None:
    module = ToolByIdentity(action_identity).reporter
    return None if module is None else cast(_Reporter, importlib.import_module(module))


def NormalizeActionResult(action_identity: str, result: DiagnosticResult, *, local: bool = False) -> DiagnosticResult:
    reporter = _reporter(action_identity)
    if reporter is None:
        return result
    normalized = reporter.NormalizeResult(
        replace(result, name=result.name.split(":")[0]),
        local=local or ToolByIdentity(action_identity).local_readiness,
    )
    return replace(normalized, name=result.name)


def ActionTextRenderer(action_identity: str) -> Callable[[Mapping[str, object]], str]:
    reporter = _reporter(action_identity)
    if reporter is None:
        raise ValueError("selected_action_has_no_text_renderer")
    return cast(_TextReporter, reporter).RenderText


def BuildReport(check_id: str, results: Sequence[DiagnosticResult]) -> dict[str, object]:
    normalized = tuple(NormalizeActionResult(check_id, result) for result in results)
    reporter = _reporter(check_id)
    checks = [_check_payload(result, reporter) for result in normalized]
    statuses = [check["status"] for check in checks]
    summary = {
        "passed": statuses.count("passed"),
        "degraded": statuses.count("degraded"),
        **{status: statuses.count(status) for status in ("timed_out", "cancelled", "skipped", "empty")},
        "failed": statuses.count("failed"),
        "total": len(statuses),
    }
    report: dict[str, object] = {
        "schema_version": "trader-tools",
        "status": "failed" if summary["failed"] or summary["timed_out"] or summary["cancelled"] else "degraded" if summary["degraded"] or summary["skipped"] else "empty" if all(status == "empty" for status in statuses) else "passed",
        "collected_at": datetime.now(_SHANGHAI).isoformat(),
        "selection": ToolByIdentity(check_id).projection(),
        "summary": summary,
        "findings": [finding for check in checks for finding in _findings(check)],
        "checks": checks,
    }
    if reporter is not None:
        reporter.CompleteReport(report, normalized)
    return report


def _check_payload(result: DiagnosticResult, reporter: _Reporter | None) -> dict[str, object]:
    original_name = result.name
    result = replace(result, name=result.name.split(":")[0])
    if (
        result.error_code is None and result.return_code == 0 and result.payload is not None
        and result.payload.get("status") == "skipped" and result.payload.get("executed") is False
    ):
        return {
            "name": original_name,
            "status": "skipped",
            "executed": False,
            "reason": result.payload.get("reason"),
            "duration_ms": 0.0,
        }
    payload: dict[str, object] = {
        "name": original_name,
        "status": ResultStatus(result),
        "executed": True,
        "duration_ms": result.duration_ms,
        "schema_version": result.payload.get("schema_version") if result.payload is not None else None,
    }
    if result.error_code is not None:
        payload["error_code"] = result.error_code
    source = result.payload or {}
    if reporter is None:
        payload["summary"] = ProjectMapping(source.get("summary"))
    else:
        reporter.ProjectDetails(result, source, payload)
    return payload


def ResultStatus(result: DiagnosticResult) -> CheckStatus:
    if result.error_code == "command_timeout":
        return "timed_out"
    if result.error_code == "command_cancelled":
        return "cancelled"
    if result.error_code is None and result.payload is not None:
        if result.payload.get("status") in ("timed_out", "cancelled"):
            return cast(CheckStatus, result.payload["status"])
    if result.error_code is not None or not result.payload or result.return_code != 0:
        return "failed"
    status = result.payload.get("status")
    if status in ("passed", "degraded", "failed", "skipped", "empty", "timed_out", "cancelled"):
        return status
    passed = result.payload.get("passed")
    return ("passed" if passed else "failed") if isinstance(passed, bool) else "failed"


def _findings(check: Mapping[str, object]) -> list[dict[str, object]]:
    existing = check.get("findings")
    if isinstance(existing, list):
        return [dict(item, check=check["name"]) for item in existing if isinstance(item, dict)][:50]
    status = check.get("status")
    if status == "passed":
        return []
    return [
        {
            "severity": "error" if status in {"failed", "timed_out", "cancelled"} else "warning",
            "code": f"{check['name']}_{status}",
            "check": check["name"],
            "evidence": check.get("summary", {"error_code": check.get("error_code")}),
        }
    ]

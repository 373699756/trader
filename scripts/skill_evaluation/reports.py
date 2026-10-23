"""Explicit JSON/Markdown projection and bounded report comparison."""

from __future__ import annotations

import json
from pathlib import Path

from .contracts import CaseResult

REPORT_VERSION = 1


def ExitCode(results: tuple[CaseResult, ...]) -> int:
    if not results or any(result.status == "unverified" for result in results):
        return 2
    return 1 if any(result.status == "failed" for result in results) else 0


def WriteReport(
    destination: Path,
    results: tuple[CaseResult, ...],
    *,
    skill_digest: str,
    suite_digest: str,
    cli_version: str,
    expected_runs: int,
    harness_digest: str = "offline-fixture",
) -> None:
    """Write only public aggregate fields; raw commands and payloads stay in case logs."""
    rows = [
        {
            "case": result.name,
            "repetition": result.repetition,
            "status": result.status,
            "elapsed_seconds": result.process.elapsed_seconds,
            "exit_code": result.process.exit_code,
            "process_problem": result.process.problem,
            "trace_problems": list(result.trace.problems),
            "actual_model": result.trace.model,
            "input_tokens": result.trace.input_tokens,
            "output_tokens": result.trace.output_tokens,
            "checks": [
                {"name": check.name, "status": check.status, "evidence": check.evidence} for check in result.checks
            ],
        }
        for result in results
    ]
    raw = {
        "report_version": REPORT_VERSION,
        "suite_digest": suite_digest,
        "skill_digest": skill_digest,
        "cli_version": cli_version,
        "harness_digest": harness_digest,
        "model_selection": "inherited Codex configuration",
        "expected_runs": expected_runs,
        "complete": len(results) == expected_runs,
        "exit_code": ExitCode(results) if len(results) == expected_runs else 2,
        "results": rows,
    }
    (destination / "summary.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# Skill evaluation",
        "",
        "Synthetic tasks only; no production or stable success-rate claim.",
        "",
        f"CLI: {cli_version}; completed {len(results)}/{expected_runs} runs.",
        "",
        "| Case | Run | Status | Evidence gaps / failures |",
        "| --- | --- | --- | --- |",
    ]
    for result in results:
        failed = [check.name for check in result.checks if check.status != "passed"]
        gaps = [*(result.trace.problems), *failed]
        if result.process.problem:
            gaps.append(result.process.problem)
        lines.append(f"| {result.name} | {result.repetition} | {result.status} | {', '.join(gaps) or 'none'} |")
    (destination / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _load(path: Path) -> dict[str, object]:
    target = path / "summary.json"
    if target.is_symlink() or target.stat().st_size > 1024 * 1024:
        raise ValueError("report must be a bounded regular JSON file")
    raw = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("report_version") != REPORT_VERSION:
        raise ValueError("unsupported report version")
    return raw


def CompareReports(before: Path, after: Path) -> str:
    """Require matching tasks, repetitions and CLI; show per-run/check changes."""
    left, right = _load(before), _load(after)
    for field in ("suite_digest", "harness_digest", "cli_version", "expected_runs"):
        if not isinstance(left.get(field), (str, int)) or left.get(field) != right.get(field):
            raise ValueError("reports have different suite, CLI or repetition settings")
    if not left.get("complete") or not right.get("complete"):
        raise ValueError("cannot compare incomplete suites")
    rows = []
    for raw in (left, right):
        values = raw.get("results")
        if not isinstance(values, list) or not all(isinstance(value, dict) for value in values):
            raise ValueError("invalid report results")
        for value in values:
            if (
                not isinstance(value.get("case"), str)
                or type(value.get("repetition")) is not int
                or value.get("status") not in ("passed", "failed", "unverified")
            ):
                raise ValueError("invalid report case identity/status")
        indexed = {(value.get("case"), value.get("repetition")): value for value in values}
        if len(indexed) != len(values):
            raise ValueError("duplicate case result")
        rows.append(indexed)
    if rows[0].keys() != rows[1].keys():
        raise ValueError("report cases do not match")
    lines = ["| Case | Run | Before | After | Changed checks |", "| --- | --- | --- | --- | --- |"]
    for key in rows[0]:
        previous, current = rows[0][key], rows[1][key]
        changed = []
        checks_by_run = []
        for value in (previous, current):
            checks = value.get("checks")
            if not isinstance(checks, list) or not all(isinstance(check, dict) for check in checks):
                raise ValueError("invalid report checks")
            checks_by_run.append(
                {
                    check["name"]: check["status"]
                    for check in checks
                    if isinstance(check.get("name"), str) and isinstance(check.get("status"), str)
                }
            )
        if checks_by_run[0].keys() != checks_by_run[1].keys():
            raise ValueError("reports have different checks")
        for name, status in checks_by_run[0].items():
            after_status = checks_by_run[1][name]
            if status != after_status:
                changed.append(f"{name}: {status} → {after_status}")
        lines.append(
            f"| {key[0]} | {key[1]} | {previous.get('status')} | {current.get('status')} | {', '.join(changed) or 'none'} |"
        )
        if previous.get("actual_model") != current.get("actual_model"):
            lines.append("\nModel identity differs; results are not a controlled model comparison.\n")
    lines.append("\nFew synthetic cases do not establish a stable success rate; null model identity means unreported.")
    return "\n".join(lines)

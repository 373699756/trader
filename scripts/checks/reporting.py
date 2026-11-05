"""Shared output and measurement helpers for internal runtime diagnostics."""

from __future__ import annotations

import json
import math
import statistics
import sys
from pathlib import Path
from collections.abc import Mapping, Sequence


def emit_report(report: Mapping[str, object]) -> None:
    """Write one machine-readable report to stdout for the public orchestrator."""
    sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


def WriteReport(report: Mapping[str, object], output: str, output_format: str = "json") -> None:
    if output_format == "text":
        from scripts.checks.download.market.market_factor_text import RenderMarketFactorText

        rendered = RenderMarketFactorText(report)
    else:
        rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if output == "-":
        sys.stdout.write(rendered)
    else:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")


def ReadJsonReports(output: str) -> tuple[dict[str, object], ...]:
    """Read complete report objects, preserving the existing tolerance of text progress lines."""
    decoder = json.JSONDecoder()
    reports: list[dict[str, object]] = []
    cursor = 0
    while cursor < len(output):
        if output[cursor].isspace():
            cursor += 1
            continue
        if output[cursor] != "{":
            newline = output.find("\n", cursor)
            if newline < 0:
                break
            cursor = newline + 1
            continue
        value, cursor = decoder.raw_decode(output, cursor)
        if not isinstance(value, dict):
            raise ValueError("diagnostic_report_must_be_object")
        reports.append(value)
    return tuple(reports)


def summarize_latency_ms(values: Sequence[float]) -> dict[str, float | int | None]:
    """Return the common nearest-rank latency summary used by source probes."""
    if not values:
        return {"sample_count": 0, "p50_ms": None, "p95_ms": None, "maximum_ms": None}
    ordered = sorted(values)
    p95_index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    return {
        "sample_count": len(ordered),
        "p50_ms": round(statistics.median(ordered), 1),
        "p95_ms": round(ordered[p95_index], 1),
        "maximum_ms": round(ordered[-1], 1),
    }

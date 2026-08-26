"""Shared output and measurement helpers for internal runtime diagnostics."""

from __future__ import annotations

import json
import math
import statistics
import sys
from collections.abc import Mapping, Sequence


def emit_report(report: Mapping[str, object]) -> None:
    """Write one machine-readable report to stdout for the public orchestrator."""
    sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


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

from __future__ import annotations

import json

from scripts.runtime_diagnostics.common import emit_report, summarize_latency_ms


def test_summarize_latency_ms_uses_nearest_rank_p95() -> None:
    assert summarize_latency_ms([10.04, 40.06, 20.05]) == {
        "sample_count": 3,
        "p50_ms": 20.1,
        "p95_ms": 40.1,
        "maximum_ms": 40.1,
    }


def test_emit_report_writes_one_json_document_to_stdout(capsys) -> None:
    emit_report({"status": "passed", "count": 2})

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"status": "passed", "count": 2}
    assert captured.out.endswith("\n")
    assert captured.err == ""

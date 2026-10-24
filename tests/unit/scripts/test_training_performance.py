"""Benchmark isolation, direct supervision, and honest comparison admission."""

from dataclasses import replace

import pytest

from scripts.diagnose_runtime import _parser, _validate, build_commands, build_report
from scripts.runtime_diagnostics.diagnostic_types import DiagnosticResult
from scripts.runtime_diagnostics.training_performance import SummarizeTrainingBenchmark


def test_benchmark_plans_direct_children_with_distinct_external_outputs(tmp_path):
    args = _parser().parse_args(
        [
            "--profile",
            "training-performance",
            "--history-root",
            str(tmp_path / "history"),
            "--training-probe-directory",
            str(tmp_path / "work"),
            "--training-profiles",
            "v1,v5",
        ]
    )
    options, _ = _validate(args)
    commands = build_commands(options)
    assert len(commands) == 5
    assert [command.name for command in commands] == [
        "training_alignment:serial_before:v1",
        "training_alignment:serial_before:v5",
        "training_alignment:parallel:v1,v5",
        "training_alignment:serial_after:v1",
        "training_alignment:serial_after:v5",
    ]
    assert all("scripts.runtime_diagnostics.training_alignment" in command.argv for command in commands)
    assert len({command.argv[command.argv.index("--work-root") + 1] for command in commands}) == 5
    (tmp_path / "work").mkdir()
    with pytest.raises(ValueError, match="new_output"):
        build_commands(options)


def test_benchmark_requires_all_rounds_and_matching_model_identity():
    profile = {
        "profile": "v5",
        "training_input_hash": "a" * 64,
        "heads": [
            {"strategy": strategy, "model_hash": "b" * 64, "report_hash": "c" * 64} for strategy in ("tomorrow", "d25")
        ],
    }
    results = tuple(
        DiagnosticResult(
            "training_alignment:" + mode + ":v5",
            0,
            duration,
            {"status": "passed", "summary": {"profiles": [profile], "process_peak_rss_bytes": 100}},
            None,
        )
        for mode, duration in (("serial_before", 10000), ("parallel", 5000), ("serial_after", 9000))
    )
    assert SummarizeTrainingBenchmark(results)["speedup_vs_best_serial"] == 1.8
    assert not SummarizeTrainingBenchmark(results[:2])["comparable"]
    blocked = replace(results[1], payload={"status": "degraded", "summary": {"profiles": []}})
    assert SummarizeTrainingBenchmark((results[0], blocked, results[2]))["speedup_vs_best_serial"] is None
    changed = replace(
        results[1],
        payload={
            "status": "passed",
            "summary": {"profiles": [dict(profile, training_input_hash="different")], "process_peak_rss_bytes": 100},
        },
    )
    mismatch = (results[0], changed, results[2])
    assert not SummarizeTrainingBenchmark(mismatch)["comparable"]
    assert build_report("training-performance", mismatch)["status"] == "degraded"

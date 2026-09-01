from __future__ import annotations

import json
from datetime import date

from scripts.codex_c_terminal_holdout import main
from trader.application.research.h1_point_in_time_completion import complete_codex_a_research
from trader.domain.research.h1_point_in_time import H1CapabilityProbe, H1PointInTimeSpec, build_h1_capability_audit
from trader.infra.research.h1_point_in_time_archive import SQLiteH1PointInTimeArchive
from trader.infra.research.h1_point_in_time_capability import H1CapabilityArtifactStore
from trader.infra.research.h1_point_in_time_completion import CodexACompletionArtifactStore
from trader.infra.research.historical_label_artifacts import HistoricalLabelArtifactStore


def _seal_codex_a_parent(root):
    capability = build_h1_capability_audit(
        (
            H1CapabilityProbe("tencent_qfq_daily", date(2024, 1, 9), False, False, "qfq", False, 640, 10, 100, 1.0),
            H1CapabilityProbe("eastmoney_historical_minute", None, False, False, "unsupported", False, 0, 0, 0, 0.0),
        ),
        probe_failures=("eastmoney_historical_minute_probe_failed",),
    )
    archive = SQLiteH1PointInTimeArchive(root / "archive")
    completion = complete_codex_a_research(
        capability=capability,
        metadata=tuple(
            archive.label_metadata(H1PointInTimeSpec(strategy)) for strategy in ("today", "tomorrow", "d25")
        ),
    )
    H1CapabilityArtifactStore(root).write(capability)
    HistoricalLabelArtifactStore(root).write(completion.labels)
    CodexACompletionArtifactStore(root).write(completion)


def test_codex_c_script_seals_three_parent_insufficient_reports_and_conclusion(tmp_path, capsys) -> None:
    parent = tmp_path / "parent"
    output = tmp_path / "output"
    _seal_codex_a_parent(parent)

    result = main(["--parent-artifact-dir", str(parent), "--output-dir", str(output), "--output", "-"])

    payload = json.loads(capsys.readouterr().out)
    assert result == 1
    assert payload["status"] == "historical_data_insufficient"
    assert payload["production_authority"] is False
    assert all(item["terminal_holdout_opened"] is False for item in payload["strategies"])
    assert all(item["status"] == "historical_data_insufficient" for item in payload["strategies"])
    assert all(item["failure_reasons"] for item in payload["strategies"])
    assert (output / "today" / "report.json").is_file()
    assert (output / "tomorrow" / "report.json").is_file()
    assert (output / "d25" / "report.json").is_file()
    assert (output / "cross_strategy" / "report.json").is_file()

    second = main(["--parent-artifact-dir", str(parent), "--output-dir", str(output), "--output", "-"])
    second_payload = json.loads(capsys.readouterr().out)
    assert second == 1
    assert second_payload["conclusion_hash"] == payload["conclusion_hash"]
    assert [item["report_hash"] for item in second_payload["strategies"]] == [
        item["report_hash"] for item in payload["strategies"]
    ]

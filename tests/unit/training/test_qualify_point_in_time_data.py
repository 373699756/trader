from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from trader.download.infra.history_archive_status import inspect_history_archive
from trader.entrypoints.cli import main as cli_main
from trader.training.application.point_in_time_data_qualification import QualifyPointInTimeData
from trader.training.domain.evaluation.historical_industry_facts import (
    HistoricalIndustrySourceContract,
    build_historical_industry_dataset_report,
    build_historical_industry_source_audit,
    merge_historical_industry_facts,
)
from trader.training.entrypoints.research_evidence_projection import project_point_in_time_data_qualification
from trader.training.infra.research import command_evidence
from trader.training.infra.research.command_evidence import HistoricalIndustryEvidenceReader
from trader.training.infra.research.h1_point_in_time_capability import FreeSourceH1CapabilityProbe

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class _Response:
    def __init__(self, payload: object) -> None:
        self._payload = payload
        self.content = b"bounded"

    def json(self) -> object:
        return self._payload

    def raise_for_status(self) -> None:
        return None


class _Session:
    def get(self, url, *, params, timeout):
        del params, timeout
        if "qq.com" in url:
            return _Response({"data": {"sh600519": {"qfqday": [["2024-01-09"]]}}})
        return _Response({"data": {"klines": []}})


def _industry_report():
    source = build_historical_industry_source_audit(
        HistoricalIndustrySourceContract(
            "baostock_archived_industry",
            "00.9.30",
            True,
            True,
            True,
            True,
            True,
            False,
            True,
        ),
        (),
        (),
    )
    return build_historical_industry_dataset_report("a" * 64, (source,), merge_historical_industry_facts(()))


def test_script_assembles_sanitized_fail_closed_report_without_downloading(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        command_evidence, "audit_archived_historical_industry_facts", lambda _root, **_kw: _industry_report()
    )
    report = QualifyPointInTimeData(
        lambda: inspect_history_archive(tmp_path, verify_partitions=True),
        HistoricalIndustryEvidenceReader(tmp_path),
        FreeSourceH1CapabilityProbe(_Session(), timeout_seconds=1.0),
    ).execute(
        code="600519",
        historical_anchor_date=date(2022, 1, 4),
    )

    payload = project_point_in_time_data_qualification(report)

    assert payload["status"] == "historical_data_insufficient"
    assert payload["point_in_time_parity"] is False
    assert payload["production_authority"] is False
    assert "600519" not in json.dumps(payload)
    assert payload["daily_archive"]["completed_codes"] == 0
    assert payload["minute_sources"][0]["supports_1450"] is False


def test_script_rejects_repository_output_path_without_writing(capsys) -> None:
    target = PROJECT_ROOT / "inside.json"

    result = cli_main(
        [
            "--config",
            str(PROJECT_ROOT / "config/runtime.json"),
            "research-data-qualification",
            "--history-root",
            str(PROJECT_ROOT / "data" / "history"),
            "--output",
            str(target),
        ]
    )

    assert result == 2
    assert not target.exists()
    assert '"status": "probe_failed"' in capsys.readouterr().out

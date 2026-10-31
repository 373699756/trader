from __future__ import annotations

from tests.helpers.configuration import SqlitePolicyFixture

import json
from datetime import date
from pathlib import Path

from trader.download.infra.history_snapshot_inspection import InspectHistorySnapshot
from trader.download.infra.history_source_capability import FreeHistorySourceCapability
from trader.entrypoints.cli import Main as cli_main
from trader.infra.http_contracts import HttpResponse
from trader.training.application.point_in_time_data_qualification import QualifyPointInTimeData
from trader.training.domain.evaluation.historical_industry_facts import (
    BuildHistoricalIndustryDatasetReport,
    BuildHistoricalIndustrySourceAudit,
    HistoricalIndustrySourceContract,
    MergeHistoricalIndustryFacts,
)
from trader.training.entrypoints.research_evidence_projection import ProjectPointInTimeDataQualification
from trader.training.infra.research import command_evidence
from trader.training.infra.research.command_evidence import HistoricalIndustryEvidenceReader
from trader.training.infra.research.h1_point_in_time_capability import FreeSourceH1CapabilityProbe

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _response(payload: object) -> HttpResponse:
    return HttpResponse(200, json.dumps(payload).encode())


class _Session:
    def get(self, url, *, params, timeout):
        del params, timeout
        if "qq.com" in url:
            return _response({"code": 0, "data": {"sh600519": {"qfqday": [["2024-01-09"]]}}})
        return _response({"rc": 0, "data": {"klines": []}})


def _industry_report():
    source = BuildHistoricalIndustrySourceAudit(
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
    return BuildHistoricalIndustryDatasetReport("a" * 64, (source,), MergeHistoricalIndustryFacts(()))


def test_script_assembles_sanitized_fail_closed_report_without_downloading(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        command_evidence, "AuditPublishedHistoricalIndustryFacts", lambda _root, **_kw: _industry_report()
    )
    report = QualifyPointInTimeData(
        lambda: InspectHistorySnapshot(tmp_path, verify_partitions=True, sqlite_policy=SqlitePolicyFixture()),
        HistoricalIndustryEvidenceReader(tmp_path, sqlite_policy=SqlitePolicyFixture()),
        FreeSourceH1CapabilityProbe(FreeHistorySourceCapability(_Session()), timeout_seconds=1.0),
    ).execute(
        code="600519",
        historical_anchor_date=date(2022, 1, 4),
    )

    payload = ProjectPointInTimeDataQualification(report)

    assert payload["status"] == "historical_data_insufficient"
    assert payload["point_in_time_parity"] is False
    assert payload["production_authority"] is False
    assert "600519" not in json.dumps(payload)
    assert payload["daily_archive"]["completed_codes"] == 0
    assert payload["minute_sources"][0]["supports_1500"] is False


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

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from trader.download.application.company_exclusions import PublishCompanyExclusions
from trader.download.infra.company_facts_index import SQLiteCompanyFactsIndex
from trader.entrypoints import cli
from trader.infra.settings import LoadRuntimeSettings
from trader.infra.settings.data_paths import DataPaths
from trader.recommendation.domain.market.eligibility import IssuerEligibilityFact, IssuerEligibilityReason
from trader.recommendation.infra.market_data.company_eligibility import CompanyEligibility


def test_eligibility_list_is_read_only_and_projects_immutable_evidence(tmp_path, monkeypatch, capsys) -> None:
    observed_at = datetime(2026, 9, 1, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    eligibility_index = CompanyEligibility(
        PublishCompanyExclusions(SQLiteCompanyFactsIndex(DataPaths(tmp_path / "data").company))
    )
    eligibility_index.record(
        (
            IssuerEligibilityFact(
                "600001",
                IssuerEligibilityReason.HISTORICAL_DELISTING_WARNING,
                observed_at,
                "quote:600001:fixture",
                "market_quote",
                "a" * 64,
            ),
        )
    )
    settings = replace(
        LoadRuntimeSettings(Path(__file__).resolve().parents[2] / "config/runtime.json"),
        config_path=tmp_path / "runtime.json",
        runtime_dir=tmp_path,
        project_root=tmp_path,
        data_paths=DataPaths(tmp_path / "data"),
        storage_overrides=(),
    )
    monkeypatch.setattr(cli, "LoadRuntimeSettings", lambda _path: settings)

    exit_code = cli.Main(
        [
            "--config",
            str(tmp_path / "runtime.json"),
            "eligibility-list",
            "--as-of",
            observed_at.isoformat(),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["schema_version"] == "issuer_eligibility_list"
    assert payload["manifest_hash"] == eligibility_index.status().manifest_hash
    assert payload["items"] == [
        {
            "code": "600001",
            "effective_at": observed_at.isoformat(),
            "evidence_hash": "a" * 64,
            "evidence_id": "quote:600001:fixture",
            "reason": "historical_delisting_warning",
            "source": "market_quote",
        }
    ]

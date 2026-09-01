from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from trader.domain.market.eligibility import IssuerEligibilityFact, IssuerEligibilityReason
from trader.entrypoints import cli
from trader.infra.persistence.issuer_eligibility import SQLiteIssuerEligibilityRegistry


def test_eligibility_list_is_read_only_and_projects_immutable_evidence(tmp_path, monkeypatch, capsys) -> None:
    observed_at = datetime(2026, 9, 1, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    registry = SQLiteIssuerEligibilityRegistry(tmp_path / "issuer-eligibility.sqlite3")
    registry.record(
        (
            IssuerEligibilityFact(
                "600001",
                IssuerEligibilityReason.HISTORICAL_ST,
                observed_at,
                "quote:600001:v1",
                "eastmoney_market",
                "a" * 64,
            ),
        )
    )
    monkeypatch.setattr(cli, "load_runtime_settings", lambda _path: SimpleNamespace(runtime_dir=tmp_path))

    exit_code = cli.main(
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
    assert payload["schema_version"] == "issuer_eligibility_list_v1"
    assert payload["manifest_hash"] == registry.status().manifest_hash
    assert payload["items"] == [
        {
            "code": "600001",
            "effective_at": observed_at.isoformat(),
            "evidence_hash": "a" * 64,
            "evidence_id": "quote:600001:v1",
            "reason": "historical_st",
            "source": "eastmoney_market",
        }
    ]

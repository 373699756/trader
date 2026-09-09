from __future__ import annotations

import json
from datetime import date

import scripts.audit_historical_industry_facts as script
from trader.domain.research.historical_industry_facts import (
    HistoricalIndustryFact,
    HistoricalIndustrySourceContract,
    HistoricalIndustryStockWindow,
    build_historical_industry_dataset_report,
    build_historical_industry_source_audit,
    merge_historical_industry_facts,
)


def _report():
    contract = HistoricalIndustrySourceContract(
        "fixture_source",
        "fixture",
        True,
        True,
        True,
        True,
        True,
        False,
        True,
    )
    fact = HistoricalIndustryFact(
        "fixture_source",
        "fixture",
        "000001",
        "银行",
        "申万一级行业",
        date(2020, 1, 1),
        None,
        None,
        "a" * 64,
    )
    window = HistoricalIndustryStockWindow("000001", "main", "old", (date(2020, 1, 2),))
    source = build_historical_industry_source_audit(contract, (fact,), (window,))
    return build_historical_industry_dataset_report("b" * 64, (source,), merge_historical_industry_facts((fact,)))


def test_projection_is_bounded_by_default_and_details_preserve_fact_hashes() -> None:
    report = _report()

    bounded = script.project_historical_industry_report(report)
    detailed = script.project_historical_industry_report(report, include_details=True)

    assert bounded["status"] == "historical_data_insufficient"
    assert bounded["production_authority"] is False
    assert bounded["cross_source_conflicts"] == 0
    assert "fact_hashes" not in bounded["sources"][0]
    assert "merged_fact_hashes" not in bounded
    assert bounded["sources"][0]["fact_hash_count"] == 1
    assert detailed["sources"][0]["fact_hashes"]
    assert detailed["merged_fact_hashes"]
    assert detailed["sources"][0]["stocks"][0]["code"] == "000001"


def test_main_rejects_detailed_stdout_without_running_audit(capsys, monkeypatch) -> None:
    monkeypatch.setattr(script, "execute", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not run")))

    result = script.main(["--include-details", "--output", "-"])

    assert result == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "audit_failed"
    assert payload["production_authority"] is False


def test_main_writes_detailed_report_only_to_an_external_path(tmp_path, monkeypatch) -> None:
    report = _report()
    target = tmp_path / "industry-report.json"
    monkeypatch.setattr(script, "PROJECT_ROOT", tmp_path / "repository")
    monkeypatch.setattr(script, "execute", lambda **_kwargs: report)

    result = script.main(["--include-details", "--output", str(target)])

    assert result == 1
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["sources"][0]["fact_hashes"] == [report.sources[0].fact_hashes[0]]
    assert payload["training_authority"] is False

"""Official excerpts preserve period and issuer identity without claiming publication."""

import json
import time
from dataclasses import replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from scripts.download.company.company_official_financial_audit import AuditCompanyOfficialFinancialSources, ProjectCompanyOfficialFinancialAudit
from scripts.download.company.cli import BuildParser, ParseOptions
from scripts.download.company.commands import BuildCommands
from scripts.core.paths import PROJECT_ROOT
from tests.helpers.download.company import company_evidence as fixtures
from trader.download.domain.company_evidence import DisclosurePage, DisclosureQuery, DisclosureSecurity


_DISCLOSED = datetime(2026, 10, 28, tzinfo=ZoneInfo("Asia/Shanghai"))
_OBSERVED = _DISCLOSED.replace(month=11, day=15)


def _baseline(tmp_path, *, period="2026-09-30", truncated=False):
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"checks": [{"name": "company_resume", "summary": {"database": {"publication": {
        "field_audit": {"financial_sources": {"latest": {"items_truncated": truncated, "items": [{
            "code": "001232", "report_date": period, "disclosed_at": int(_DISCLOSED.timestamp()) * 1000000,
            "fields": [{"field": "core_profit_growth_pct", "reason": "source_null"}],
        }]}}},
    }}}}]}))
    return path


class _Source:
    def __init__(self, *, more=False, wrong_code=False):
        self.more, self.wrong_code, self.partitions = more, wrong_code, []

    def split_queries(self, partition):
        return (DisclosureQuery("szmb", (DisclosureSecurity("001232", "org"),)),)

    def fetch(self, partition):
        self.partitions.append(partition)
        row = replace(fixtures._disclosure("quarter-report", "2026年第三季度报告", when=_DISCLOSED),
                      codes=("001233" if self.wrong_code else "001232",))
        return DisclosurePage((row,), 31 if self.more else 1, self.more)

    def download_document(self, row):
        pass

    def extract_document(self, row):
        return fixtures._extraction("证券代码：001232\n主要会计数据和财务指标\n本报告期 本报告期比上年同期增减 年初至报告期末 年初至报告期末比上年同期增减\n归属于上市公司股东的扣除非经常性损益的净利润 5 10% 15 不适用\n")


def test_official_same_period_excerpts_preserve_source_and_leave_null_classification_unverified(tmp_path):
    source = _Source()
    result = AuditCompanyOfficialFinancialSources(source, _baseline(tmp_path), observed_at=_OBSERVED, deadline=time.monotonic() + 10)
    probe = ProjectCompanyOfficialFinancialAudit(result["official_financial_probe"])
    assert result["status"] == "degraded" and probe["production_adopted"] is False
    item, = probe["items"]
    assert item["baseline_disclosed_at"] == _DISCLOSED.isoformat()
    material, = item["sources"]
    assert material["source_key"] == "quarter-report" and material["issuer_codes"] == ["001232"]
    assert "不适用" in material["excerpts"][0]["matches"][0]["quote"]
    assert source.partitions[0].start == date(2026, 10, 27) and source.partitions[0].query.securities[0].code == "001232"


@pytest.mark.parametrize("kind", ("more", "wrong_code", "deadline"))
def test_incomplete_directory_wrong_issuer_and_exhausted_budget_never_produce_a_proof(tmp_path, kind):
    source = _Source(more=kind == "more", wrong_code=kind == "wrong_code")
    result = AuditCompanyOfficialFinancialSources(source, _baseline(tmp_path), observed_at=_OBSERVED,
                                                  deadline=time.monotonic() + (-1 if kind == "deadline" else 10))
    item, = result["official_financial_probe"]["items"]
    assert item["sources"] == [] and item["status"] == "verification_required"


@pytest.mark.parametrize("period,truncated", (("2026-06-30", False), ("2026-09-30", True)))
def test_incomplete_baseline_or_unsupported_period_cannot_be_silently_audited(tmp_path, period, truncated):
    with pytest.raises(ValueError):
        AuditCompanyOfficialFinancialSources(_Source(), _baseline(tmp_path, period=period, truncated=truncated),
                                              observed_at=_OBSERVED, deadline=time.monotonic() + 10)


def test_official_audit_public_entry_requires_explicit_external_archive_and_keeps_selection(tmp_path):
    baseline = _baseline(tmp_path)
    archive = tmp_path / "official-materials"
    options = ParseOptions(BuildParser("research-source-compare").parse_args([
        "--official-financial-baseline", str(baseline), "--official-financial-directory", str(archive),
    ]))
    command, = BuildCommands(options)
    assert command.argv[-4:] == ("--official-financial-baseline", str(baseline), "--official-financial-directory", str(archive))
    assert not archive.exists()
    with pytest.raises(ValueError, match="requires --official-financial-directory"):
        ParseOptions(BuildParser("research-source-compare").parse_args(["--official-financial-baseline", str(baseline)]))
    with pytest.raises(ValueError, match="outside the repository"):
        ParseOptions(BuildParser("research-source-compare").parse_args([
            "--official-financial-baseline", str(baseline), "--official-financial-directory", str(PROJECT_ROOT / "data"),
        ]))


def test_public_financial_projection_cannot_forward_arbitrary_supplier_payloads():
    private = "private-unrelated-provider-payload"
    result = ProjectCompanyOfficialFinancialAudit({
        "stocks": {"private": private}, "fields": True, "unknown": private,
        "items": [{"code": "001232", "fields": [{"private": private}, "eps"], "sources": [{
            "issuer_codes": [private, "001232"] * 20, "unknown": private,
            "table_excerpts": [{"field": "eps", "matches": [{"start_line": {"private": private}, "quote": "0.00", "unknown": private}]}],
        }]}],
    })
    assert private not in json.dumps(result)
    assert result["stocks"] is None and result["fields"] is None

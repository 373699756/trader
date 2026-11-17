"""Official excerpts preserve period and issuer identity without claiming publication."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from scripts.download.company.company_official_financial_audit import ProjectCompanyOfficialFinancialAudit
from scripts.download.company.cli import BuildParser, ParseOptions
from scripts.download.company.commands import BuildCommands
from scripts.core.paths import PROJECT_ROOT


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

"""Free alternatives keep report basis, bank fields and missing disclosure identity honest."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from scripts.download.company import company_financial_alternatives as audit
from scripts.download.company.cli import BuildParser, ParseOptions
from scripts.download.company.commands import BuildCommands
from trader.infra.http_contracts import HttpResponse

_NOW = datetime(2026, 11, 18, 12, tzinfo=ZoneInfo("Asia/Shanghai"))


def _sina(*, bank=False):
    values = [(1, "EPSBASIC", "0.85"), (1, "NAPS", "15.9691"), (4, "TAGRT", "16.234513"),
              (4, "NPGRT", "-23.708128"), (1, "NPCUT", "2813682000"), (1, "ROEWEIGHTED", "5.34"),
              (1, "NETPARECOMPPROF" if bank else "PARENETP", "4060242000")]
    rows = [{"item_group_no": group, "item_field": field, "item_value": value,
             "item_tongbi": -0.27464, "item_number": "GJ1", "item_title": "归母净利润", "item_source": "lrb"}
            for group, field, value in values]
    # Equal display labels in another group must not override cumulative weighted ROE.
    rows.append({"item_group_no": 3, "item_field": "ROEWEIGHTED", "item_value": "900"})
    return {"result": {"status": {"code": 0}, "data": {"report_list": {"20260930": {
        "rType": "合并期末", "rCurrency": "CNY", "publish_date": "20261027", "data": rows,
    }}}}}


def _ths():
    values = {"basic_eps": "0.85", "calc_per_net_assets": "15.9691",
              "calculate_operating_income_total_yoy_growth_ratio": "16.234513",
              "calculate_parent_holder_net_profit_yoy_growth_ratio": "-23.708128",
              "deduct_net_profit_yoy_growth_ratio": "-27.4642", "index_weighted_avg_roe": "5.34",
              "parent_holder_net_profit": "4060242000", "index_deduct_holder_net_profit": "2813682000"}
    return {"status_code": 0, "data": {"page": 1, "size": 2, "data": [{
        "date": "2026-09-30", "report": "2026-3",
        "index_list": {field: {"value": value, "single": "900"} for field, value in values.items()},
    }]}}


class _Session:
    def __init__(self, *, bank=False, changed=False):
        self.bank, self.changed, self.requests = bank, changed, []

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        payload = _sina(bank=self.bank) if "sina" in url else _ths()
        if "sina" not in url and self.changed:
            payload["data"]["data"][0]["index_list"]["calc_per_net_assets"]["value"] = "15.83"
        return HttpResponse(200, json.dumps(payload).encode())


@pytest.mark.parametrize("bank", (False, True))
def test_sina_bank_mapping_and_ths_cumulative_values_are_comparable_without_inventing_disclosure(bank):
    session = _Session(bank=bank)
    report = audit.AuditCompanyFinancialAlternatives(session, ("000063",), observed_at=_NOW, timeout_seconds=8,
                                                   deadline=60, http_attempts=[], monotonic=lambda: 0)
    assert report["status"] == "passed" and report["production_adopted"] is False
    first, second = report["samples"]
    assert first["reports"][0]["metrics"]["parent_net_profit"] == 4060242000
    assert first["reports"][0]["metrics"]["roe_pct"] == second["reports"][0]["metrics"]["roe_pct"] == 5.34
    assert first["reports"][0]["disclosed_on"] == "2026-10-27"
    assert second["reports"][0]["disclosed_on"] is None and second["reports"][0]["currency"] is None
    assert report["comparisons"][0]["comparable_fields"] == 8 and report["comparisons"][0]["mismatched_fields"] == 0
    assert session.requests[0][1]["params"]["paperCode"] == "sz000063"
    assert session.requests[1][1]["params"]["market"] == "33"
    assert len(session.requests) == 2


def test_real_mismatch_is_not_averaged_or_filled_from_another_source():
    report = audit.AuditCompanyFinancialAlternatives(_Session(changed=True), ("000063",), observed_at=_NOW, timeout_seconds=8,
                                                   deadline=60, http_attempts=[], monotonic=lambda: 0)
    assert report["comparisons"][0]["differences"] == ["book_value_per_share"]
    assert report["samples"][0]["reports"][0]["metrics"]["book_value_per_share"] == 15.9691
    assert report["samples"][1]["reports"][0]["metrics"]["book_value_per_share"] == 15.83


def test_one_denied_source_keeps_the_other_observation_without_exposing_the_response():
    class Denied(_Session):
        def get(self, url, **kwargs):
            if "sina" in url:
                self.requests.append((url, kwargs))
                return HttpResponse(403, b"private-provider-response")
            return super().get(url, **kwargs)

    session = Denied()
    report = audit.AuditCompanyFinancialAlternatives(session, ("000063",), observed_at=_NOW, timeout_seconds=8,
                                                   deadline=60, http_attempts=[], monotonic=lambda: 0)
    assert report["status"] == "degraded" and len(session.requests) == 2
    assert report["samples"][0]["failure"] == "access_denied" and report["samples"][1]["status"] == "decoded"
    assert "private-provider-response" not in json.dumps(report)


def test_unknown_basis_and_duplicate_bank_field_are_rejected():
    payload = _sina(bank=True)
    rows = payload["result"]["data"]["report_list"]["20260930"]["data"]
    rows.append({"item_group_no": 1, "item_field": "PARENETP", "item_value": "1"})
    with pytest.raises(ValueError, match="duplicate_metric"):
        audit._sina_reports(payload, "600036")
    payload = _sina()
    payload["result"]["data"]["report_list"]["20260930"]["rType"] = "单季"
    with pytest.raises(ValueError, match="basis_unverified"):
        audit._sina_reports(payload, "600600")


def test_source_command_limits_codes_and_excludes_other_modes_and_raw_payload():
    options = ParseOptions(BuildParser("research-source-compare").parse_args([
        "--financial-alternatives", "--codes", "600600", "000063",
    ]))
    command, = BuildCommands(options)
    assert command.argv[-4:] == ("--financial-alternatives", "--codes", "600600", "000063")
    for codes in ((), ("600600", "600600"), ("00700",), tuple(str(600000 + index) for index in range(9))):
        with pytest.raises(ValueError):
            audit.ValidateCompanyFinancialAlternativeCodes(codes)
    with pytest.raises(SystemExit):
        ParseOptions(BuildParser("research-source-compare").parse_args(["--financial-alternatives", "--structured-window", "financial"]))
    assert "private-payload" not in json.dumps(audit.ProjectCompanyFinancialAlternatives({"samples": [{"source": "sina", "private": "private-payload"}]}))

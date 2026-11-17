"""Official directory metadata remains separate from original bodies and financial facts."""

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from scripts.download.company.company_official_directory_audit import AuditCompanyOfficialDirectoryPages, ProbeCompanyOfficialCinfo
from trader.download.domain.company_source_records import CompanyOfficialDirectoryQuery
from trader.download.infra.company_official_directory import DecodeCompanyOfficialDirectory
from trader.infra.http_contracts import HttpAttempt, HttpConnectionError, HttpResponse

_AT = datetime(2026, 11, 17, tzinfo=ZoneInfo("Asia/Shanghai"))
_QUERY = CompanyOfficialDirectoryQuery(date(2026, 10, 27), date(2026, 10, 27), "010307")


def _row():
    return {"id": "12345678-abcd-abcd-abcd-123456789012", "annId": 1220000000, "secCode": ["000001"],
            "title": "公司2026年第三季度报告", "publishTime": "2026-10-27 00:00:00", "attachFormat": "PDF",
            "attachPath": "/disc/disk03/finalpage/2026-10-27/12345678-abcd-abcd-abcd-123456789012.pdf",
            "attachSize": 999, "content": None, "private_unknown": "not-a-fact"}


def _decode(rows, total=None):
    return DecodeCompanyOfficialDirectory(json.dumps({"announceCount": len(rows) if total is None else total,
                                                     "data": rows}).encode(), _QUERY, observed_at=_AT)


def test_metadata_preserves_both_identities_without_inventing_size_units_or_originals():
    page = _decode([_row()])
    item, = page.observations
    assert item.identity == _row()["id"] and item.announcement_id == 1220000000
    assert item.attachment_path.startswith("/disc/") and item.codes == ("000001",)
    assert not page.has_more and len(page.response_hash) == 64
    assert not hasattr(item, "body_hash") and not hasattr(item, "revision") and not hasattr(item, "attachment_bytes")


@pytest.mark.parametrize("key,value", (("id", "new-format"), ("annId", True), ("secCode", ["600001"]),
                                      ("publishTime", "2026-10-28 00:00:00"), ("attachPath", "https://foreign.test/a.pdf")))
def test_unrecognized_identity_window_or_attachment_never_becomes_a_directory_observation(key, value):
    row = _row()
    row[key] = value
    with pytest.raises(ValueError):
        _decode([row])


def test_duplicate_identity_is_rejected_and_partial_page_does_not_claim_a_complete_sample():
    with pytest.raises(ValueError, match="duplicate_identity"):
        _decode([_row(), _row()])
    with pytest.raises(ValueError, match="count_invalid"):
        _decode([_row()], 506)


def test_empty_result_is_a_directory_page_without_asserting_risk_or_financial_coverage():
    page = _decode([])
    assert page.total == 0 and not page.observations and not page.has_more
    report = AuditCompanyOfficialDirectoryPages((page,))
    assert report["status"] == "observed_metadata_only" and len(report["unqualified_families"]) == 4
    assert not report["full_market_verified"] and not report["field_correctness_verified"]
    assert not report["production_adopted"] and report["documents_downloaded"] == 0


class _StaticSession:
    def __init__(self, attempts, *, fail=False):
        self.calls, self.attempts, self.fail = [], attempts, fail

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        self.attempts.append(HttpAttempt("english.sse.com.cn", "GET", 1.0, 0, None, "HttpConnectionError" if self.fail else ""))
        if self.fail:
            raise HttpConnectionError("private_network_error")
        return HttpResponse(200, b'<script src="/actual.js"></script><script src="https://foreign.test/private.js"></script>')


def test_static_probe_has_one_explicit_bounded_request_and_only_observed_official_script_links(tmp_path):
    attempts = []
    source = _StaticSession(attempts)
    report = ProbeCompanyOfficialCinfo(source, tmp_path / "output", attempts, observed_at=_AT, deadline=15,
                                     monotonic=lambda: 0, protected_roots=(tmp_path / "protected",))
    assert len(source.calls) == report["transport_attempts"] == 1
    assert source.calls[0][1]["deadline"] == source.calls[0][1]["timeout"] == 15
    assert source.calls[0][1]["max_response_bytes"] == 2 * 1024 * 1024
    assert report["script_urls"] == ["https://english.sse.com.cn/actual.js"]
    assert report["status"] == "observed_static" and not report["full_market_verified"]


def test_static_probe_failure_is_saved_without_retry_and_an_existing_intent_cannot_repeat(tmp_path):
    attempts = []
    source = _StaticSession(attempts, fail=True)
    directory = tmp_path / "output"
    report = ProbeCompanyOfficialCinfo(source, directory, attempts, observed_at=_AT, deadline=15,
                                     monotonic=lambda: 0, protected_roots=(tmp_path / "protected",))
    assert len(source.calls) == 1 and report["failure"] == "HttpConnectionError"
    assert "private_network_error" not in json.dumps(report)
    with pytest.raises(ValueError, match="boundary_invalid"):
        ProbeCompanyOfficialCinfo(source, directory, attempts, observed_at=_AT, deadline=15,
                                 monotonic=lambda: 0, protected_roots=(tmp_path / "protected",))
    assert len(source.calls) == 1 and (directory / "transport-receipt.json").is_file()

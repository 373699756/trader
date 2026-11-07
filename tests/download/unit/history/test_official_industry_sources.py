from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from scripts.core.paths import PROJECT_ROOT
from scripts.download.history import official_industry as audit_module
from scripts.download.history.official_industry import AuditOfficialIndustrySources, OfficialIndustryAuditRequest
from trader.download.infra.official_industry_pdf import OfficialIndustryRow, OfficialIndustrySnapshot
from trader.download.infra.official_industry_release import ParseOfficialIndustryRelease, ValidateOfficialIndustryUrl
from trader.infra.http_contracts import HttpCancelled, HttpResponse, HttpTimeout

_ANNOUNCEMENT = "https://www.capco.org.cn/dataFile/zsxfb/hyfl/hyfljg/2e1b516684544cc7be7eab6f93f7dddc.js"
_ATTACHMENT = "https://sp.capco.org.cn:82/file/202609/gwfile/2026shangbangupiaodaima.pdf"
_OBSERVED_AT = datetime(2026, 11, 6, 20, tzinfo=ZoneInfo("Asia/Shanghai"))
_PDF = b"%PDF-1.7\nfixture"


def _announcement(
    *,
    date_text: str = "2026-09-30",
    link: str = _ATTACHMENT,
    title: str = "2026年上半年上市公司行业分类结果",
    extra_text: str = "",
) -> str:
    return "export const allData = " + json.dumps({
        "date": date_text,
        "source": "中国上市公司协会",
        "title": title,
        "text": (
            f'<p><a href="{link}\r\n">{title}（按股票代码排序）.pdf</a></p>'
            '<a href="https://sp.capco.org.cn:82/file/202609/gwfile/2026shangbanhangyefenlei.pdf">'
            f'{title}（按行业排序）.pdf</a>{extra_text}'
        ),
    }, ensure_ascii=False)


class _Transport:
    def __init__(self, responses: dict[str, HttpResponse | Exception]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, float, float | None, int]] = []
        self.on_request: Callable[[], None] = lambda: None
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def get(
        self,
        url: str,
        *,
        timeout: float,
        headers: dict[str, str] | None = None,
        deadline: float | None,
        max_response_bytes: int,
        cancel_requested: Callable[[], bool],
    ) -> HttpResponse:
        self.requests.append((url, timeout, deadline, max_response_bytes))
        self.on_request()
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def parser(monkeypatch):
    calls = []

    def parse(content, period_end, *, cancel_requested):
        assert not cancel_requested()
        assert content == _PDF
        calls.append(period_end)
        return OfficialIndustrySnapshot(
            (
                OfficialIndustryRow("000001", "平安银行", "J66", "货币金融服务", 1, "J", "金融业"),
                OfficialIndustryRow("200512", "闽灿坤B", "C38", "电气机械和器材制造业", 2, "C", "制造业", "CH", "电气、电子及通讯"),
            ),
            2,
        )

    monkeypatch.setattr(audit_module, "ParseOfficialIndustryPdf", parse)
    return calls


def _transport() -> _Transport:
    return _Transport({_ANNOUNCEMENT: HttpResponse(200, _announcement().encode()), _ATTACHMENT: HttpResponse(200, _PDF)})


def _audit(root: Path, transport: _Transport, **kwargs):
    return AuditOfficialIndustrySources(
        OfficialIndustryAuditRequest((_ANNOUNCEMENT,), root, timeout_seconds=15, budget_seconds=40),
        transport,
        clock=lambda: _OBSERVED_AT,
        monotonic_clock=lambda: 100,
        **kwargs,
    )


def _source(report):
    return report["summary"]["sources"][0]


def test_capco_structured_release_selects_code_sorted_attachment_and_preserves_dates():
    release = ParseOfficialIndustryRelease(_ANNOUNCEMENT, _announcement())
    assert release.period_end == date(2026, 6, 30)
    assert release.disclosed_on == date(2026, 9, 30)
    assert release.conservative_available_on == date(2026, 10, 1)
    assert release.native_classification == "中国上市公司协会上市公司行业分类"
    assert release.attachment_url == _ATTACHMENT


@pytest.mark.parametrize(
    "content",
    (
        _announcement().replace('"date":', '"other_date":'),
        _announcement() + ";alert('must not evaluate')",
        _announcement().replace("export const allData = ", "window.allData = "),
        _announcement().replace("中国上市公司协会", "other-source"),
        _announcement(extra_text=(
            '<a href="https://sp.capco.org.cn:82/file/202609/gwfile/other.pdf">'
            '2026年上半年上市公司行业分类结果（按股票代码排序）.pdf</a>'
        )),
        _announcement(title="行业分类"),
        _announcement(date_text="2026-06-29"),
        _announcement(date_text="2026-09-31"),
        _announcement(link="https://other.example/industry.pdf"),
        _announcement(link="http://sp.capco.org.cn:82/file/202609/gwfile/industry.pdf"),
    ),
)
def test_ambiguous_missing_or_unsafe_release_evidence_is_rejected(content):
    with pytest.raises(ValueError):
        ParseOfficialIndustryRelease(_ANNOUNCEMENT, content)


@pytest.mark.parametrize(
    "url",
    (
        "http://www.capco.org.cn/dataFile/zsxfb/hyfl/hyfljg/2e1b516684544cc7be7eab6f93f7dddc.js",
        "https://www.capco.org.cn.evil.example/dataFile/zsxfb/hyfl/hyfljg/release.js",
        "https://www.capco.org.cn/dataFile/zsxfb/hyfl/hyfljg/../release.js",
        _ANNOUNCEMENT + "?download=1",
        _ANNOUNCEMENT + "#attachment",
        "https://www.capco.org.cn/dataFile/zsxfb/other/release.js",
    ),
)
def test_nonallowlisted_announcements_are_rejected_before_network(url):
    with pytest.raises(ValueError, match="official_industry_url_invalid"):
        ValidateOfficialIndustryUrl(url)


def test_cold_candidate_preserves_evidence_and_warm_reuses_without_network(tmp_path, parser):
    transport = _transport()
    cold = _audit(tmp_path, transport)
    assert cold["status"] == "degraded"
    assert cold["summary"]["network_requests"] == 2
    assert cold["summary"]["production_eligible"] is False
    assert cold["summary"]["window_coverage_verified"] is False
    assert cold["summary"]["source_data_writes"] == 0
    source = _source(cold)
    assert source["status"] == "parsed"
    assert source["disclosed_on"] == "2026-09-30"
    assert source["announcement_sha256"] == hashlib.sha256(_announcement().encode()).hexdigest()
    assert source["attachment_sha256"] == hashlib.sha256(_PDF).hexdigest()
    assert source["row_count"] == 2 and source["b_share_rows"] == 1
    rows = json.loads((tmp_path / source["rows_file"]).read_text())
    assert rows[0]["code"] == "000001" and rows[0]["native_category"] == "J66"
    assert rows[0]["section_code"] == "J" and rows[0]["subcategory_code"] == ""
    assert rows[1]["section_code"] == "C" and rows[1]["section_name"] == "制造业"
    assert rows[1]["subcategory_code"] == "CH" and rows[1]["subcategory_name"] == "电气、电子及通讯"
    assert transport.requests == [
        (_ANNOUNCEMENT, 15, 140, 2 * 1024 * 1024),
        (_ATTACHMENT, 15, 140, 32 * 1024 * 1024),
    ]
    transport.requests.clear()
    transport.on_request = lambda: pytest.fail("warm evidence must not perform network requests")
    warm = _audit(tmp_path, transport)
    assert _source(warm)["attachment_cache_hit"] is True
    assert _source(warm)["received_at"] == source["received_at"]
    assert warm["summary"]["cache_hits"] == 2
    assert warm["summary"]["network_requests"] == 0
    assert parser == [date(2026, 6, 30)] * 2


def test_failed_attachment_preserves_announcement_and_resumes_only_missing_document(tmp_path, parser):
    transport = _transport()
    transport.responses[_ATTACHMENT] = HttpTimeout("fixture_timeout")
    failed = _audit(tmp_path, transport)
    assert failed["status"] == "failed"
    assert _source(failed)["failure"] == "HttpTimeout"
    assert parser == []
    announcement_digest = hashlib.sha256(_announcement().encode()).hexdigest()
    assert (tmp_path / (announcement_digest + ".blob")).read_bytes() == _announcement().encode()
    transport.responses[_ATTACHMENT] = HttpResponse(200, _PDF)
    transport.requests.clear()
    resumed = _audit(tmp_path, transport)
    assert _source(resumed)["status"] == "parsed"
    assert resumed["summary"]["cache_hits"] == 1
    assert [request[0] for request in transport.requests] == [_ATTACHMENT]


@pytest.mark.parametrize("damage", ("blob", "receipt", "future_receipt"))
def test_cache_corruption_fails_without_requery_or_cleanup(tmp_path, parser, damage):
    transport = _transport()
    _audit(tmp_path, transport)
    receipt = tmp_path / (hashlib.sha256(_ANNOUNCEMENT.encode()).hexdigest() + ".json")
    blob = tmp_path / (hashlib.sha256(_announcement().encode()).hexdigest() + ".blob")
    if damage == "blob":
        blob.write_bytes(b"tampered")
    elif damage == "receipt":
        receipt.write_text('{"url":"unexpected"}')
    else:
        payload = json.loads(receipt.read_text())
        payload["received_at"] = "2027-01-01T00:00:00+08:00"
        receipt.write_text(json.dumps(payload))
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.suffix != ".lock"}
    transport.requests.clear()
    result = _audit(tmp_path, transport)
    assert result["status"] == "failed"
    assert _source(result)["reason"] == "official_industry_cache_invalid"
    assert transport.requests == []
    assert before == {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.suffix != ".lock"}


def test_future_disclosure_never_fetches_attachment(tmp_path, parser):
    transport = _transport()
    transport.responses[_ANNOUNCEMENT] = HttpResponse(200, _announcement(date_text="2027-01-01").encode())
    result = _audit(tmp_path, transport)
    assert _source(result)["reason"] == "official_industry_future_disclosure"
    assert [request[0] for request in transport.requests] == [_ANNOUNCEMENT]
    assert parser == []


def test_preexisting_cancel_and_exhausted_budget_perform_no_network(tmp_path, parser):
    transport = _transport()
    cancelled = _audit(tmp_path, transport, cancel_requested=lambda: True)
    assert _source(cancelled)["failure"] == "HttpCancelled"
    readings = iter((100, 141))
    timed_out = AuditOfficialIndustrySources(
        OfficialIndustryAuditRequest((_ANNOUNCEMENT,), tmp_path, budget_seconds=40),
        transport,
        clock=lambda: _OBSERVED_AT,
        monotonic_clock=lambda: next(readings, 141),
    )
    assert _source(timed_out)["failure"] == "HttpTimeout"
    assert transport.requests == [] and parser == []
    assert not list(tmp_path.glob("*.blob"))


def test_cancel_during_request_releases_lock_and_does_not_cache_response(tmp_path, parser):
    cancelled = False
    transport = _transport()

    def cancel():
        nonlocal cancelled
        cancelled = True

    transport.on_request = cancel
    result = _audit(tmp_path, transport, cancel_requested=lambda: cancelled)
    assert _source(result)["failure"] == "HttpCancelled"
    assert not list(tmp_path.glob("*.blob"))
    cancelled = False
    transport.on_request = lambda: None
    resumed = _audit(tmp_path, transport)
    assert _source(resumed)["status"] == "parsed"


def test_parser_cancel_releases_lock_and_preserves_downloads(tmp_path, parser, monkeypatch):
    transport = _transport()

    def cancelled_parser(*args, **kwargs):
        raise HttpCancelled("official_industry_cancelled")

    original_parser = audit_module.ParseOfficialIndustryPdf
    monkeypatch.setattr(audit_module, "ParseOfficialIndustryPdf", cancelled_parser)
    result = _audit(tmp_path, transport)
    assert _source(result)["failure"] == "HttpCancelled"
    assert len(list(tmp_path.glob("*.blob"))) == 2
    monkeypatch.setattr(audit_module, "ParseOfficialIndustryPdf", original_parser)
    transport.requests.clear()
    assert _source(_audit(tmp_path, transport))["status"] == "parsed"
    assert transport.requests == []


def test_pdf_wrong_period_stays_failed_and_preserves_original(tmp_path, parser, monkeypatch):
    def wrong_period(*args, **kwargs):
        raise ValueError("official_industry_pdf_period_mismatch")

    monkeypatch.setattr(audit_module, "ParseOfficialIndustryPdf", wrong_period)
    result = _audit(tmp_path, _transport())
    assert _source(result)["reason"] == "official_industry_pdf_period_mismatch"
    assert len(list(tmp_path.glob("*.blob"))) == 2
    assert not list(tmp_path.glob("*.rows.json"))


def test_work_root_inside_checkout_is_rejected_before_creation_or_network(parser):
    transport = _transport()
    root = PROJECT_ROOT / ".runtime" / "official-industry-forbidden-test"
    with pytest.raises(ValueError, match="outside the repository"):
        _audit(root, transport)
    assert not root.exists()
    assert transport.requests == []


@pytest.mark.parametrize("status", (302, 403, 500))
def test_http_status_is_not_accepted_as_valid_document(tmp_path, parser, status):
    transport = _transport()
    transport.responses[_ANNOUNCEMENT] = HttpResponse(status, _announcement().encode())
    result = _audit(tmp_path, transport)
    assert _source(result)["failure"] == "HttpStatusError"
    assert _source(result)["http_status"] == status
    assert not list(tmp_path.glob("*.blob"))


def test_public_tool_command_forwards_official_mode_without_loading_runtime(tmp_path, monkeypatch):
    import trader.infra.settings as settings_module
    from scripts.download.history import cli, commands

    monkeypatch.setattr(
        settings_module, "LoadRuntimeSettings", lambda *args: pytest.fail("official mode must not read runtime data"),
    )
    options = cli.ParseOptions(cli.BuildParser("history-industry").parse_args([
        "--official-announcements", _ANNOUNCEMENT, "--industry-work-root", str(tmp_path),
        "--source-timeout-seconds", "12", "--command-timeout-seconds", "80",
    ]))
    command, = commands.BuildCommands(options, python_executable="/python3")
    assert "--history-root" not in command.argv
    assert "--runtime-config" not in command.argv
    assert command.argv[command.argv.index("--official-announcements") + 1] == _ANNOUNCEMENT
    assert command.argv[command.argv.index("--industry-work-root") + 1] == str(tmp_path)
    assert command.argv[command.argv.index("--budget-seconds") + 1] == "80.0"
    assert command.argv[command.argv.index("--timeout-seconds") + 1] == "12.0"


@pytest.mark.parametrize("arguments", (("--official-announcements", _ANNOUNCEMENT), ("--industry-work-root", "/tmp")))
def test_public_tool_requires_both_official_source_and_external_work_root(arguments):
    from scripts.download.history import cli

    with pytest.raises(ValueError, match="must be set together"):
        cli.ParseOptions(cli.BuildParser("history-industry").parse_args(arguments))


def test_public_tool_rejects_checkout_as_official_work_root():
    from scripts.download.history import cli

    with pytest.raises(ValueError, match="outside the repository"):
        cli.ParseOptions(cli.BuildParser("history-industry").parse_args([
            "--official-announcements", _ANNOUNCEMENT, "--industry-work-root", str(PROJECT_ROOT),
        ]))


def test_worker_official_mode_closes_transport_without_opening_runtime_or_history(tmp_path, parser, monkeypatch):
    import trader.bootstrap as bootstrap
    import trader.infra.settings as settings_module
    from scripts.download.history import history_industry_preparation as worker

    transport = _transport()
    reports = []
    monkeypatch.setattr(bootstrap, "OpenExternalHttp", lambda **kwargs: transport)
    monkeypatch.setattr(
        settings_module, "LoadRuntimeSettings", lambda *args: pytest.fail("official mode must not read runtime data"),
    )
    monkeypatch.setattr(worker, "emit_report", reports.append)
    monkeypatch.setattr(sys, "argv", [
        "industry", "--official-announcements", _ANNOUNCEMENT, "--industry-work-root", str(tmp_path),
    ])
    assert worker.Main() == 0
    assert reports[0]["status"] == "degraded"
    assert reports[0]["summary"]["production_eligible"] is False
    assert transport.closed

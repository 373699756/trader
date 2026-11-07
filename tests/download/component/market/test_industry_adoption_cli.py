"""Offline CLI adoption proof; suppliers and company delivery are explicit fixtures.

The real CLI, reference preparation, industry parser/publication and SQLite owners
run twice. This is neither a live supplier nor a complete company performance test.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import pytest

from tests.helpers.shared.reference_fixtures import PreparedFixture
from trader import bootstrap
from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.domain.company_evidence import (
    CompanyEvidenceRequest,
    CompanyEvidenceSyncResult,
    EvidenceSyncProgress,
)
from trader.download.infra.current_industry_publication import CurrentIndustryCache
from trader.download.infra.industry_query_index import IndustrySourceDocument, SQLiteIndustryQueryIndex
from trader.download.infra.reference_index import SQLiteReferenceIndex
from trader.entrypoints import cli
from trader.infra.http_contracts import HttpAttempt, HttpRequest, HttpResponse
from trader.infra.http_transport import RequestsHttpTransport
from trader.infra.settings import LoadRuntimeSettings
from trader.infra.settings.data_paths import DataPaths

_AT = datetime(2026, 11, 7, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
_START = date(2024, 1, 1)
_STANDARD = "中国上市公司协会上市公司行业分类标准"
_TAXONOMY_KEY = "cninfo:p_public0002:008001"


def _response(records: tuple[dict[str, object], ...]) -> HttpResponse:
    content = json.dumps(
        {"resultcode": 200, "count": len(records), "records": records},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return HttpResponse(200, content)


def _taxonomy_response() -> HttpResponse:
    records: tuple[dict[str, object], ...] = tuple(
        {
            "SORTCODE": code,
            "PARENTCODE": parent,
            "SORTNAME": name,
            "F001V": None,
            "F002D": None,
            "F003V": "008001",
            "F004V": _STANDARD,
        }
        for code, parent, name in (
            ("0", "008", "中上协行业分类"),
            ("J", "0", "金融业"),
            ("J66", "J", "货币金融服务"),
            ("J661", "J66", "中央银行服务"),
        )
    )
    return _response(records)


def _stock_response(code: str) -> HttpResponse:
    record: dict[str, object] = {
        "SECCODE": code,
        "SECNAME": "离线股份",
        "ORGNAME": "离线股份有限公司",
        "VARYDATE": "2024-09-30",
        "F001V": "008001",
        "F002V": _STANDARD,
        "F003V": "J661",
        "F004V": "金融业",
        "F005V": None,
        "F006V": "货币金融服务",
        "F007V": "中央银行服务",
        "F008C": "1",
    }
    return _response((record,))


def test_normal_sync_cli_commits_hdd_64_plus_one_and_reuses_the_complete_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_root = Path(__file__).resolve().parents[4]
    runtime = replace(
        LoadRuntimeSettings(project_root / "config/runtime.json"),
        data_paths=DataPaths(tmp_path / "data"),
        runtime_dir=tmp_path / "runtime",
        disk_medium="hdd",
    )
    codes = tuple(f"{600001 + index:06d}" for index in range(65))
    population = tuple(
        BaoStockSecurity(code, "离线股份", "main", date(1999, 1, 1), None, "fixture-official")
        for code in codes
    )
    reference = PreparedFixture(_AT, population, open_dates=(_START, _AT.date()))
    sqlite_policy = runtime.sqlite_policy.hdd
    industry_path = runtime.data_paths.reference / "industry.sqlite3"
    batches: list[tuple[str, ...]] = []
    supplier_calls: list[tuple[str, str]] = []
    company_calls: list[tuple[str, ...]] = []
    save_documents = SQLiteIndustryQueryIndex.save_source_documents

    def respond(request: HttpRequest) -> HttpResponse:
        parsed = urlsplit(request.url)
        endpoint = parsed.path.rsplit("/", 1)[-1]
        query = parse_qs(parsed.query)
        code = query.get("scode", [""])[0]
        supplier_calls.append((endpoint, code))
        if endpoint == "p_public0002":
            assert code == ""
            return _taxonomy_response()
        assert endpoint == "p_stock2110" and code in codes
        assert query["sdate"] == [_START.isoformat()]
        assert query["edate"] == [_AT.date().isoformat()]
        return _stock_response(code)

    def open_http(
        *,
        observe: Callable[[HttpAttempt], None] | None = None,
        inspect_response: Callable[[HttpRequest, HttpResponse], None] | None = None,
        identity_content_encodings: tuple[str, ...] = (),
        execute_request: Callable[[HttpRequest], HttpResponse] | None = None,
    ) -> RequestsHttpTransport:
        assert execute_request is None
        return RequestsHttpTransport(
            observe=observe,
            inspect_response=inspect_response,
            identity_content_encodings=identity_content_encodings,
            execute_request=respond,
        )

    def save(self: SQLiteIndustryQueryIndex, documents: tuple[IndustrySourceDocument, ...]) -> None:
        assert self.path == industry_path
        save_documents(self, documents)
        batches.append(tuple(document.request_key for document in documents))

    def company(
        paths: DataPaths,
        _progress: Callable[[EvidenceSyncProgress], None] | None,
        request: CompanyEvidenceRequest,
        **_options: object,
    ) -> CompanyEvidenceSyncResult:
        assert paths == runtime.data_paths and request.codes == codes
        published = SQLiteReferenceIndex(paths.reference_database, sqlite_policy=sqlite_policy).read()
        assert published is not None and published.industry_version is not None
        assert tuple(security.code for security in published.eligible_universe) == codes
        company_calls.append(request.codes)
        return CompanyEvidenceSyncResult(_AT, len(codes), len(codes), 0, 0, 0, 0, (), 0.0)

    monkeypatch.setattr(cli, "LoadRuntimeSettings", lambda _path: runtime)
    monkeypatch.setattr(bootstrap, "LoadRuntimeSettings", lambda _path: runtime)
    monkeypatch.setattr(bootstrap, "_utc_now", lambda: _AT)
    monkeypatch.setattr(bootstrap, "LoadCurrentAShareUniverse", lambda *_args, **_kwargs: population)
    monkeypatch.setattr(bootstrap.HistoryStNameSource, "fetch", lambda *_args: reference.st_evidence)
    monkeypatch.setattr(bootstrap, "FetchAkshareCalendar", lambda *_args, **_kwargs: reference.open_dates)
    monkeypatch.setattr(bootstrap, "OpenExternalHttp", open_http)
    monkeypatch.setattr(bootstrap, "RecentPublishedNewsCodes", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(bootstrap, "_sync_company_evidence", company)
    monkeypatch.setattr(SQLiteIndustryQueryIndex, "save_source_documents", save)

    arguments = ["--config", str(runtime.config_path), "sync", "company"]
    assert cli.Main(arguments) == 0
    first = capsys.readouterr()
    result = json.loads(first.out)
    assert result["status"] == "completed" and result["failure"] is None
    assert result["tasks"][0]["task"] == "company" and result["tasks"][0]["status"] == "completed"
    assert "有效单批上限 64股" in first.err
    assert "正文合计上限 2097152字节" in first.err
    assert "参考·参考发布" in first.err and "sync·结果汇总" in first.err
    assert [len(batch) for batch in batches] == [1, 64, 1]
    assert batches[0] == (_TAXONOMY_KEY,)
    assert tuple(key for batch in batches[1:] for key in batch) == tuple(
        "cninfo:p_stock2110:" + code for code in codes
    )
    assert supplier_calls == [("p_public0002", ""), *(("p_stock2110", code) for code in codes)]

    published_reference = SQLiteReferenceIndex(
        runtime.data_paths.reference_database, sqlite_policy=sqlite_policy,
    ).read()
    assert published_reference is not None
    assert published_reference.official_universe == population
    assert published_reference.st_evidence == reference.st_evidence
    assert published_reference.open_dates == reference.open_dates
    observations = CurrentIndustryCache(industry_path, sqlite_policy=sqlite_policy).load(_AT)
    assert tuple(item.subject_key for item in observations) == codes
    assert {item.data_version for item in observations} == {published_reference.industry_version}
    index = SQLiteIndustryQueryIndex(industry_path, "cninfo_industry_queries", sqlite_policy=sqlite_policy)
    taxonomy = index.read_source_document(_TAXONOMY_KEY, _AT)
    assert taxonomy is not None and taxonomy.content == _taxonomy_response().content
    for observation in observations:
        document = index.read_source_document("cninfo:p_stock2110:" + observation.subject_key, _AT)
        expected = _stock_response(observation.subject_key).content
        assert document is not None and document.content == expected
        assert document.content_hash == hashlib.sha256(expected).hexdigest() == observation.payload_hash
        assert document.requested_from == _START and document.requested_to == _AT.date()

    batches.clear()
    supplier_calls.clear()
    assert cli.Main(arguments) == 0
    second = capsys.readouterr()
    assert json.loads(second.out)["status"] == "completed"
    assert "当前发布缓存复用 65 股" in second.err
    assert "参考·参考发布" in second.err and "sync·结果汇总" in second.err
    assert supplier_calls == [] and batches == []
    assert CurrentIndustryCache(industry_path, sqlite_policy=sqlite_policy).load(_AT) == observations
    assert SQLiteReferenceIndex(runtime.data_paths.reference_database, sqlite_policy=sqlite_policy).read() == published_reference
    assert company_calls == [codes, codes]
    assert not runtime.data_paths.company.exists()

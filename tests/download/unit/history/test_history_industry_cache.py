import hashlib
import json
from dataclasses import replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.helpers.shared.configuration import SqlitePolicyFixture
from trader.download.domain.industry_evidence import IndustryCategoryMapping, OfficialIndustryEvidence
from trader.download.infra.cninfo_industry import ParseCninfoIndustryChanges
from trader.download.infra.industry_evidence_codec import IndustryEvidencePayload, LoadReviewedIndustryEvidence
from trader.download.infra.industry_query_index import IndustrySourceDocument, SQLiteIndustryQueryIndex
from trader.download.application.prepare_reference import IndustryDatabaseIncompatibleError
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction

_ZONE = ZoneInfo("Asia/Shanghai")
_OBSERVED_AT = datetime(2026, 11, 6, 20, tzinfo=_ZONE)
_FIRST_DATE = date(2020, 1, 1)
_LAST_DATE = date(2026, 11, 6)


def _index(path):
    return SQLiteIndustryQueryIndex(path / "industry.sqlite3", "cninfo_industry_queries", sqlite_policy=SqlitePolicyFixture())


def _document(code="001222", *, latest="1"):
    record = {
        "SECCODE": code, "SECNAME": "测试股份", "ORGNAME": "测试股份有限公司", "VARYDATE": "2024-09-30",
        "F001V": "008001", "F002V": "中国上市公司协会上市公司行业分类标准", "F003V": "C243",
        "F004V": "制造业", "F005V": "造纸、印刷、文教", "F006V": "文教、工美、体育和娱乐用品制造业",
        "F007V": "工艺美术及礼仪用品制造", "F008C": latest,
    }
    content = json.dumps({"resultcode": 200, "count": 1, "records": [record]}, ensure_ascii=False, indent=2).encode()
    return IndustrySourceDocument("cninfo:p_stock2110:" + code, _FIRST_DATE, _LAST_DATE, _OBSERVED_AT, content)


def _official_evidence():
    return OfficialIndustryEvidence(
        "600003", "https://disc.static.szse.cn/fixture.pdf", "a" * 64,
        datetime(2026, 11, 2, 12, tzinfo=_ZONE), date(2026, 11, 3), date(2026, 11, 6),
        datetime(2026, 11, 2, 18, tzinfo=_ZONE), 1, "CAPCO2023 C29 Plastics",
        IndustryCategoryMapping(
            "CAPCO2023", "C29", "Plastics", "C29Plastics", "https://www.csrc.gov.cn/fixture.pdf",
            "b" * 64, 1, "CSRC2012 C29Plastics", "Reviewed category scope in both standards.",
        ),
    )


def test_original_response_round_trip_retains_bytes_request_window_and_digest(tmp_path):
    index = _index(tmp_path)
    original = _document()
    index.save_source_document(original)
    reopened = _index(tmp_path)
    assert reopened.read_source_document(original.request_key, _OBSERVED_AT) == original
    assert original.content_hash == hashlib.sha256(original.content).hexdigest()
    with SqliteSession(index.path) as connection:
        row = connection.run("SELECT payload,content_hash FROM industry_source_document").require_one()
    assert row[0] == original.content
    assert row[1] == hashlib.sha256(original.content).digest()


@pytest.mark.parametrize("latest", (0, 1, "0", "1"))
def test_original_latest_marker_survives_cache_without_sdk_field_projection(tmp_path, latest):
    index = _index(tmp_path)
    original = _document(latest=latest)
    index.save_source_document(original)
    document = index.read_source_document(original.request_key, _OBSERVED_AT)
    assert document is not None
    parsed = ParseCninfoIndustryChanges(document.content, "001222", document.requested_from, document.requested_to)
    row, = parsed.rows
    assert row.latest_marker == latest and type(row.latest_marker) is type(latest)
    assert parsed.response_content == original.content


@pytest.mark.parametrize("fields", (
    {"requested_from": _FIRST_DATE + timedelta(days=1)},
    {"requested_to": _LAST_DATE - timedelta(days=1)},
    {"received_at": _OBSERVED_AT - timedelta(hours=1)},
))
def test_new_candidate_cannot_shrink_coverage_or_replace_a_newer_receipt(tmp_path, fields):
    index = _index(tmp_path)
    original = _document()
    index.save_source_document(original)
    with pytest.raises(ValueError, match="would_shrink"):
        index.save_source_document(replace(original, **fields))
    assert _index(tmp_path).read_source_document(original.request_key, _OBSERVED_AT) == original


def test_complete_refresh_retains_previous_original_and_as_of_reads(tmp_path):
    index = _index(tmp_path)
    original = replace(_document(), requested_from=date(2024, 1, 1), requested_to=date(2026, 11, 5))
    index.save_source_document(original)
    extended = replace(_document(latest=1), received_at=_OBSERVED_AT + timedelta(minutes=1))
    index.save_source_document(extended)
    assert index.read_source_document(extended.request_key, extended.received_at) == extended
    assert index.read_source_document(extended.request_key, original.received_at) == original
    with SqliteSession(index.path) as connection:
        rows = connection.run("SELECT payload,received_at FROM industry_source_document ORDER BY received_at").fetchall()
    assert len(rows) == 2
    assert tuple(row[0] for row in rows) == (original.content, extended.content)


@pytest.mark.parametrize("field", ("content", "window"))
def test_same_receipt_time_conflict_is_rejected_without_overwriting_either_original(tmp_path, field):
    index = _index(tmp_path)
    original = _document()
    index.save_source_document(original)
    conflict = (
        replace(original, content=original.content + b"\n")
        if field == "content" else replace(original, requested_from=_FIRST_DATE - timedelta(days=1))
    )
    with pytest.raises(ValueError, match="time_conflict"):
        index.save_source_document(conflict)
    assert index.read_source_document(original.request_key, _OBSERVED_AT) == original
    with SqliteSession(index.path) as connection:
        assert connection.run("SELECT COUNT(*) FROM industry_source_document").require_one()[0] == 1


def test_resume_retains_completed_raw_documents_and_only_adds_the_missing_key(tmp_path):
    first = _document()
    index = _index(tmp_path)
    index.save_source_document(first)
    with pytest.raises(ValueError, match="document_invalid"):
        replace(_document("002594"), content=b"")
    second = _document("002594")
    reopened = _index(tmp_path)
    assert reopened.read_source_document(first.request_key, _OBSERVED_AT) == first
    assert reopened.read_source_document(second.request_key, _OBSERVED_AT) is None
    reopened.save_source_document(second)
    assert reopened.read_source_document(first.request_key, _OBSERVED_AT) == first
    assert reopened.read_source_document(second.request_key, _OBSERVED_AT) == second


def test_future_receipt_is_unavailable_at_an_earlier_observation_without_rewriting(tmp_path):
    index = _index(tmp_path)
    original = _document()
    index.save_source_document(original)
    assert index.read_source_document(original.request_key, _OBSERVED_AT - timedelta(seconds=1)) is None
    assert index.read_source_document(original.request_key, _OBSERVED_AT) == original


@pytest.mark.parametrize("fields", (
    {"requested_to": date(2026, 11, 7)},
    {"received_at": datetime(2026, 11, 6, 20)},
    {"request_key": "cninfo:p_stock2110:"},
    {"requested_from": date(2026, 11, 7)},
))
def test_invalid_future_or_unbound_source_documents_are_rejected_before_writing(tmp_path, fields):
    index = _index(tmp_path)
    with pytest.raises(ValueError, match="document_invalid"):
        index.save_source_document(replace(_document(), **fields))
    assert not index.path.exists()


@pytest.mark.parametrize("damage", ("payload", "digest"))
def test_corrupt_original_is_rejected_without_requery_delete_or_digest_repair(tmp_path, damage):
    index = _index(tmp_path)
    original = _document()
    index.save_source_document(original)
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        if damage == "payload":
            connection.run("UPDATE industry_source_document SET payload=?", (b"altered",))
        else:
            connection.run("UPDATE industry_source_document SET content_hash=?", (b"0" * 32,))
    before = index.path.read_bytes()
    with pytest.raises(ValueError, match="hash_conflict"):
        index.read_source_document(original.request_key, _OBSERVED_AT)
    assert index.path.read_bytes() == before


@pytest.mark.parametrize("field", ("requested_start", "received_at"))
def test_source_receipt_metadata_cannot_change_without_invalidating_its_digest(tmp_path, field):
    index = _index(tmp_path)
    original = _document()
    index.save_source_document(original)
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        if field == "requested_start":
            connection.run(
                "UPDATE industry_source_document SET requested_start=?",
                ((_FIRST_DATE - timedelta(days=1)).isoformat(),),
            )
        else:
            connection.run(
                "UPDATE industry_source_document SET received_at=?",
                ((_OBSERVED_AT - timedelta(hours=1)).isoformat(),),
            )
        row = connection.run("SELECT payload,content_hash FROM industry_source_document").require_one()
        assert row[0] == original.content
        assert row[1] == hashlib.sha256(original.content).digest()
    before = index.path.read_bytes()
    with pytest.raises(ValueError, match="hash_conflict"):
        index.read_source_document(original.request_key, _OBSERVED_AT)
    assert index.path.read_bytes() == before


def test_missing_source_database_read_does_not_create_a_database_or_parent(tmp_path):
    index = _index(tmp_path / "missing")
    assert index.read_source_document(_document().request_key, _OBSERVED_AT) is None
    assert not index.path.parent.exists()


def test_unreadable_cache_is_explicitly_rejected_and_kept_intact(tmp_path):
    index = _index(tmp_path)
    index.path.write_bytes(b"not a database")
    with pytest.raises(RuntimeError, match="industry_query_cache_unavailable"):
        index.read_source_document(_document().request_key, _OBSERVED_AT)
    with pytest.raises(RuntimeError, match="industry_query_cache_unavailable"):
        index.save_source_document(_document())
    assert index.path.read_bytes() == b"not a database"


def test_existing_schema_without_source_documents_is_rejected_without_upgrade(tmp_path):
    index = _index(tmp_path)
    index.save("fixture", _FIRST_DATE, _FIRST_DATE, "[]")
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        original = tuple(connection.run("SELECT * FROM current_industry_query").require_one())
        connection.run("DROP TABLE industry_source_document")
    before = index.path.read_bytes()
    for failure, reason, operation in (
        (RuntimeError, "industry_query_cache_unavailable", lambda: index.read_source_document(_document().request_key, _OBSERVED_AT)),
        (IndustryDatabaseIncompatibleError, "industry_database_incompatible", lambda: index.save_source_document(_document())),
        (IndustryDatabaseIncompatibleError, "industry_database_incompatible", index.prepare),
    ):
        with pytest.raises(failure, match=reason):
            operation()
        assert index.path.read_bytes() == before
    with SqliteSession(index.path) as connection:
        assert tuple(connection.run("SELECT * FROM current_industry_query").require_one()) == original
        assert connection.run("SELECT name FROM sqlite_schema WHERE name='industry_source_document'").fetchone() is None


def test_industry_evidence_conflicts_are_atomic_and_corruption_is_rejected(tmp_path):
    index = _index(tmp_path)
    original = _official_evidence()
    index.import_evidence((original,))
    conflict = replace(original, mapping=replace(original.mapping, target_industry="C29Other"))
    with pytest.raises(ValueError, match="interval_conflict"):
        index.import_evidence((replace(original, code="600004"), conflict))
    at = original.received_at + timedelta(days=2)
    assert index.read_evidence(at) == (original,)
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        connection.run("UPDATE official_industry_evidence SET code='600005'")
    with pytest.raises(ValueError, match="hash_conflict"):
        index.read_evidence(at)


@pytest.mark.parametrize("cancelled", (True, False))
def test_reviewed_import_rejects_future_receipt_and_cancellation_before_database_write(tmp_path, monkeypatch, cancelled):
    import trader.download.infra.industry_query_index as module

    index = _index(tmp_path)
    evidence = _official_evidence()
    monkeypatch.setattr(module, "LoadReviewedIndustryEvidence", lambda _: (evidence,))
    if cancelled:
        assert not index.import_reviewed_file(tmp_path / "input.json", observed_at=evidence.disclosed_at, cancelled=lambda: True)
    else:
        with pytest.raises(ValueError, match="received_in_future"):
            index.import_reviewed_file(tmp_path / "input.json", observed_at=evidence.disclosed_at, cancelled=lambda: False)
    assert not index.path.exists()


@pytest.mark.parametrize("invalid", ("issuer", "excerpt", "mapping", "path", "url", "valid"))
def test_reviewed_import_rejects_wrong_subject_or_missing_document_evidence(tmp_path, monkeypatch, invalid):
    import trader.download.infra.industry_evidence_codec as codec

    class _Page:
        def __init__(self, text):
            self.text = text

        def extract_text(self):
            return self.text

    class _Pdf:
        def __init__(self, text):
            self.pages = (_Page(text),)
            self.closed = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.closed = True

    source = _Pdf("股票代码：600003 CAPCO2023 C29 Plastics")
    mapping = _Pdf("CSRC2012 C29Plastics")
    evidence = _official_evidence()
    raw = IndustryEvidencePayload(evidence)
    if invalid == "issuer":
        source.pages[0].text = "股票代码：600009 CAPCO2023 C29 Plastics"
    elif invalid == "excerpt":
        source.pages[0].text = "股票代码：600003 missing"
    elif invalid == "mapping":
        mapping.pages[0].text = "C29Other"
    elif invalid == "url":
        raw["source_url"] = "https://example.com/unofficial.pdf"
    monkeypatch.setattr(codec, "_pdf", lambda root, name, digest: source if name == "issuer.pdf" else mapping)
    manifest = [{"evidence": raw, "document": "issuer.pdf", "mapping_document": "mapping.pdf"}]
    path = tmp_path / "evidence.json"
    if invalid == "path":
        monkeypatch.undo()
        manifest[0]["document"] = "../outside.pdf"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    if invalid == "valid":
        assert LoadReviewedIndustryEvidence(path) == (evidence,)
    else:
        with pytest.raises(ValueError):
            LoadReviewedIndustryEvidence(path)
    if invalid not in {"path", "url"}:
        assert source.closed and mapping.closed

import hashlib
import json
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.helpers.shared.configuration import SqlitePolicyFixture
from trader.download.domain.industry_evidence import OfficialIndustryEvidence
from trader.download.infra.cninfo_industry import ParseCninfoIndustryChanges
from trader.download.infra.industry_evidence_codec import IndustryEvidencePayload, ParseIndustryEvidence
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
        "600003",
        "https://sp.capco.org.cn:82/file/202609/fixture.pdf",
        "a" * 64,
        date(2026, 11, 2),
        date(2026, 11, 3),
        date(2026, 11, 6),
        datetime(2026, 11, 2, 18, tzinfo=_ZONE),
        1,
        "600003 C29 塑料制品业",
        "C29",
        "塑料制品业",
        "https://www.capco.org.cn/dataFile/zsxfb/hyfl/hyfljg/" + "b" * 32 + ".js",
        "b" * 64,
    )

def test_original_response_round_trip_retains_bytes_request_window_and_digest(tmp_path):
    index = _index(tmp_path)
    original = _document()
    index.save_source_documents((original,))
    reopened = _index(tmp_path)
    assert reopened.read_source_document(original.request_key, _OBSERVED_AT) == original
    assert original.content_hash == hashlib.sha256(original.content).hexdigest()
    with SqliteSession(index.path) as connection:
        row = connection.run("SELECT payload,content_hash FROM industry_source_document").require_one()
    assert row[0] == original.content
    assert row[1] == hashlib.sha256(original.content).digest()


def test_two_originals_commit_together_and_retain_exact_bytes_and_receipts(tmp_path, monkeypatch):
    import trader.download.infra.industry_query_index as module

    index = _index(tmp_path)
    index.prepare()
    original_session = module.SqliteSession
    statements = []

    @contextmanager
    def traced_session(path, options, *, sqlite_policy):
        with original_session(path, options, sqlite_policy=sqlite_policy) as connection:
            connection.observe_statements(statements.append)
            yield connection

    monkeypatch.setattr(module, "SqliteSession", traced_session)
    documents = (_document("001222"), _document("002594"))
    index.save_source_documents(documents)
    assert statements.count("BEGIN") == statements.count("COMMIT") == 1
    monkeypatch.undo()
    reopened = _index(tmp_path)
    assert tuple(reopened.read_source_document(item.request_key, _OBSERVED_AT) for item in documents) == documents
    with SqliteSession(index.path) as connection:
        rows = connection.run(
            "SELECT request_key,payload,content_hash,receipt_hash FROM industry_source_document ORDER BY request_key"
        ).fetchall()
    assert tuple(tuple(row) for row in rows) == tuple(
        (item.request_key, item.content, bytes.fromhex(item.content_hash), bytes.fromhex(item.receipt_hash))
        for item in documents
    )


def test_conflict_in_second_original_rolls_back_the_first_and_keeps_previous_receipt(tmp_path):
    index = _index(tmp_path)
    original = _document("002594")
    index.save_source_documents((original,))
    first = _document("001222")
    conflict = replace(original, content=original.content + b"\n")
    with pytest.raises(ValueError, match="time_conflict"):
        index.save_source_documents((first, conflict))
    reopened = _index(tmp_path)
    assert reopened.read_source_document(first.request_key, _OBSERVED_AT) is None
    assert reopened.read_source_document(original.request_key, _OBSERVED_AT) == original


@pytest.mark.parametrize("invalid", ("empty", "list", "member", "duplicate", "rows", "bytes"))
def test_invalid_source_batches_are_rejected_before_creating_a_database(tmp_path, invalid):
    index = _index(tmp_path)
    original = _document()
    if invalid == "empty":
        documents = ()
    elif invalid == "list":
        documents = [original]
    elif invalid == "member":
        documents = (None,)
    elif invalid == "duplicate":
        documents = (original, replace(original, received_at=_OBSERVED_AT + timedelta(seconds=1)))
    elif invalid == "rows":
        documents = tuple(_document(f"{600001 + number:06d}") for number in range(index.source_batch_rows + 1))
    else:
        documents = (replace(original, content=b"x" * index.source_batch_bytes), _document("002594"))
    with pytest.raises(ValueError, match="industry_source_batch"):
        index.save_source_documents(documents)
    assert not index.path.exists()


@pytest.mark.parametrize("configured_rows", (1, 2, 64, 128))
def test_source_batch_uses_the_selected_policy_with_a_fixed_upper_bound(tmp_path, configured_rows):
    index = replace(_index(tmp_path), sqlite_policy=replace(SqlitePolicyFixture(), transaction_batch_rows=configured_rows))
    assert index.source_batch_rows == min(configured_rows, 64)
    assert index.source_batch_bytes == 2 * 1024 * 1024
    documents = tuple(_document(f"{600001 + number:06d}") for number in range(index.source_batch_rows))
    index.save_source_documents(documents)
    with SqliteSession(index.path) as connection:
        assert connection.run("SELECT COUNT(*) FROM industry_source_document").require_one()[0] == len(documents)


def test_one_original_at_the_exact_batch_byte_limit_remains_legal(tmp_path):
    index = _index(tmp_path)
    original = replace(_document(), content=b"x" * index.source_batch_bytes)
    index.save_source_documents((original,))
    assert _index(tmp_path).read_source_document(original.request_key, _OBSERVED_AT) == original


@pytest.mark.parametrize("latest", (0, 1, "0", "1"))
def test_original_latest_marker_survives_cache_without_sdk_field_projection(tmp_path, latest):
    index = _index(tmp_path)
    original = _document(latest=latest)
    index.save_source_documents((original,))
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
    index.save_source_documents((original,))
    with pytest.raises(ValueError, match="would_shrink"):
        index.save_source_documents((replace(original, **fields),))
    assert _index(tmp_path).read_source_document(original.request_key, _OBSERVED_AT) == original


def test_complete_refresh_retains_previous_original_and_as_of_reads(tmp_path):
    index = _index(tmp_path)
    original = replace(_document(), requested_from=date(2024, 1, 1), requested_to=date(2026, 11, 5))
    index.save_source_documents((original,))
    extended = replace(_document(latest=1), received_at=_OBSERVED_AT + timedelta(minutes=1))
    index.save_source_documents((extended,))
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
    index.save_source_documents((original,))
    conflict = (
        replace(original, content=original.content + b"\n")
        if field == "content" else replace(original, requested_from=_FIRST_DATE - timedelta(days=1))
    )
    with pytest.raises(ValueError, match="time_conflict"):
        index.save_source_documents((conflict,))
    assert index.read_source_document(original.request_key, _OBSERVED_AT) == original
    with SqliteSession(index.path) as connection:
        assert connection.run("SELECT COUNT(*) FROM industry_source_document").require_one()[0] == 1


def test_resume_retains_completed_raw_documents_and_only_adds_the_missing_key(tmp_path):
    first = _document()
    index = _index(tmp_path)
    index.save_source_documents((first,))
    with pytest.raises(ValueError, match="document_invalid"):
        replace(_document("002594"), content=b"")
    second = _document("002594")
    reopened = _index(tmp_path)
    assert reopened.read_source_document(first.request_key, _OBSERVED_AT) == first
    assert reopened.read_source_document(second.request_key, _OBSERVED_AT) is None
    reopened.save_source_documents((second,))
    assert reopened.read_source_document(first.request_key, _OBSERVED_AT) == first
    assert reopened.read_source_document(second.request_key, _OBSERVED_AT) == second


def test_future_receipt_is_unavailable_at_an_earlier_observation_without_rewriting(tmp_path):
    index = _index(tmp_path)
    original = _document()
    index.save_source_documents((original,))
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
        index.save_source_documents((replace(_document(), **fields),))
    assert not index.path.exists()


@pytest.mark.parametrize("damage", ("payload", "digest"))
def test_corrupt_original_is_rejected_without_requery_delete_or_digest_repair(tmp_path, damage):
    index = _index(tmp_path)
    original = _document()
    index.save_source_documents((original,))
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
    index.save_source_documents((original,))
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
        index.save_source_documents((_document(),))
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
        (IndustryDatabaseIncompatibleError, "industry_database_incompatible", lambda: index.save_source_documents((_document(),))),
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
    conflict = replace(
        original,
        native_name="其他橡胶和塑料制品业",
        document_sha256="c" * 64,
        announcement_sha256="d" * 64,
        announcement_url="https://www.capco.org.cn/dataFile/zsxfb/hyfl/hyfljg/" + "d" * 32 + ".js",
    )
    with pytest.raises(ValueError, match="interval_conflict"):
        index.import_evidence((replace(original, code="600004"), conflict))
    at = original.received_at + timedelta(days=4)
    assert index.read_evidence_window(date(2026, 11, 3), date(2026, 11, 5), at) == (original,)
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        connection.run("UPDATE official_industry_evidence SET code='600005'")
    with pytest.raises(ValueError, match="hash_conflict"):
        index.read_evidence_window(date(2026, 11, 3), date(2026, 11, 5), at)


def test_current_official_codec_preserves_date_precision_and_native_identity():
    original = _official_evidence()
    payload = IndustryEvidencePayload(original)
    assert payload["disclosed_on"] == "2026-11-02"
    assert "disclosed_at" not in payload and "mapping" not in payload
    assert ParseIndustryEvidence(payload) == original
    assert original.industry == "capco:C29塑料制品业"


@pytest.mark.parametrize("field", ("source_url", "announcement_url"))
def test_current_official_codec_rejects_nonofficial_sources(field):
    payload = IndustryEvidencePayload(_official_evidence())
    payload[field] = "https://example.com/unofficial.pdf"
    with pytest.raises(ValueError, match="official_industry_url_invalid"):
        ParseIndustryEvidence(payload)


def test_current_official_codec_rejects_retired_mapping_payload():
    payload = IndustryEvidencePayload(_official_evidence())
    payload["mapping"] = {"target_industry": "C29Plastics"}
    with pytest.raises(ValueError, match="industry_evidence_fields_invalid"):
        ParseIndustryEvidence(payload)


@pytest.mark.parametrize("start", (date(2026, 11, 1), date(2026, 11, 2)))
def test_official_date_precision_never_backdates_public_availability(start):
    with pytest.raises(ValueError, match="official_industry_evidence_invalid"):
        replace(_official_evidence(), effective_from=start)

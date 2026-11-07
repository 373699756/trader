"""Published source fields share fact identity, transaction and restart semantics."""

import hashlib
from dataclasses import replace
from datetime import timedelta

import pytest

from tests.helpers.download.company import company_evidence as fixtures
from tests.helpers.download.company_resources import CompanyDocumentFilesFixture, SQLiteCompanyEvidenceReaderFixture, SQLiteCompanyFactsIndexFixture
from trader.download.domain.company_event_extraction import ExtractCompanyLinkCandidate
from trader.download.domain.company_events import CompanyPositiveStatus, CompanySubject
from trader.download.domain.company_evidence import CompanyDocument, DisclosurePage, DisclosurePartition
from trader.download.infra import company_facts_index, company_field_publication
from trader.download.infra.company_fact_rows import ReadCompanyFactRows
from trader.download.infra.company_facts_index import CompanyInputReceipt, ReadCompanyPublicationIdentity
from trader.download.infra.company_field_publication import PrepareCompanyFieldResults
from trader.download.infra.company_field_result_rows import ReadCompanyFieldResults
from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession


_AT = fixtures.NOW
_DETAIL = "本公告仅说明本次事项的实际执行情况。相关安排已经正式披露，公司将根据实际执行情况及时披露后续进度。"
_TEXT = (
    "证券代码：600001\n本公告披露本次回购的实际执行情况，相关信息请以正式公告为准。\n"
    "本公司首次回购股份100万股，占公司总股本的比例为0.3%。\n"
    "回购股份用于注销。\n" + _DETAIL
)


def _put(index, text=_TEXT, *, identity="field-delivery", digest="field-record", title="关于首次回购公司股份的公告"):
    item = fixtures._disclosure(identity, title, digest=digest)
    partition = DisclosurePartition("sse", item.published_at.date(), item.published_at.date())
    index.commit_page(partition, DisclosurePage((item,), 1, False), _AT)
    extraction = fixtures._extraction(text)
    assert extraction.text == text, "the fixture must not duplicate the actual statement to meet a text minimum"
    assert index.commit_document(item, extraction, _AT)
    return CompanyDocument(item, text, True)


def _reader(index):
    return SQLiteCompanyEvidenceReaderFixture(index.facts_path)


def _identity(index):
    with SqliteSession(index.facts_path, SqliteConnectionOptions(read_only=True)) as connection:
        return ReadCompanyPublicationIdentity(connection)


def _receipts(index, *, ordinary_only=False):
    with SqliteSession(index.facts_path, SqliteConnectionOptions(read_only=True)) as connection:
        return tuple(tuple(row) for row in connection.run(
            "SELECT * FROM company_input_receipt "
            + ("WHERE outcome='valid' " if ordinary_only else "")
            + "ORDER BY source,source_key,code,record_hash,extraction_hash"
        ))


def _logical_state(index):
    with SqliteSession(index.facts_path, SqliteConnectionOptions(read_only=True)) as connection:
        return (
            ReadCompanyPublicationIdentity(connection),
            ReadCompanyFactRows(connection, ("600001",)),
            ReadCompanyFieldResults(connection, ("600001",)),
            tuple(tuple(row) for row in connection.run(
                "SELECT * FROM company_input_receipt ORDER BY source,source_key,code,record_hash,extraction_hash"
            )),
        )


def _prepare(index, facts, documents):
    with SqliteSession(index.path, SqliteConnectionOptions(read_only=True)) as collected, SqliteSession(
        index.facts_path, SqliteConnectionOptions(read_only=True),
    ) as formal:
        return PrepareCompanyFieldResults(
            collected,
            formal,
            facts,
            documents,
            observed_at=_AT,
            extraction_hash=ReadCompanyPublicationIdentity(formal).extraction_hash,
            event_policy=fixtures.CompanyEventPolicy(),
            read_document=CompanyDocumentFilesFixture(index.path.parent / "text").read,
        )


def _unproved_publication(tmp_path):
    """A processed legacy input has ordinary valid, never an invented field proof."""
    index = fixtures._index(tmp_path)
    index.publish(("600001",), _AT)
    document = _put(index)
    before, = _reader(index).read(("600001",))
    candidate = ExtractCompanyLinkCandidate(document, _AT, fixtures.CompanyEventPolicy())
    facts = replace(before, positive_events=(candidate.positive,), review_reasons=(), pending_document_count=0)
    writer = SQLiteCompanyFactsIndexFixture(index.facts_path)
    writer.publish(
        (facts,),
        {document.disclosure.identity: document.disclosure.source},
        _identity(index),
        _AT,
        receipts=(CompanyInputReceipt(
            document.disclosure.source,
            document.disclosure.identity,
            facts.code,
            document.disclosure.digest,
            writer.extraction_hash,
            hashlib.sha256(document.text.encode()).digest(),
            _AT,
        ),),
    )
    assert {item.state for item in _reader(index).read_fields((facts.code,)).results} == {"present_unverified"}
    return index, document, facts


def test_source_fields_publish_with_exact_text_subject_and_the_ordinary_fact_identity(tmp_path):
    index = fixtures._index(tmp_path)
    document = _put(index)
    index.publish(document.disclosure.codes, _AT)
    reader = _reader(index)
    before = reader.read(document.disclosure.codes)
    snapshot = reader.read_fields(document.disclosure.codes)
    after = reader.read(document.disclosure.codes)

    assert before == after
    assert snapshot.publication_hash == reader.publication_version()
    assert {item.field for item in snapshot.results} == {"actual_pct", "repurchase_purpose"}
    assert all(item.state == "verified" and item.applicable for item in snapshot.results)
    actual = next(item for item in snapshot.results if item.field == "actual_pct")
    purpose = next(item for item in snapshot.results if item.field == "repurchase_purpose")
    assert actual.value == before[0].positive_events[0].measurements.actual_ratio_pct == 0.3
    assert purpose.value == "cancellation"
    assert before[0].positive_events[0].subject_type is CompanySubject.ISSUER
    for result in snapshot.results:
        evidence, = result.evidence
        assert evidence.source_key == document.disclosure.identity
        assert evidence.record_hash == document.disclosure.digest
        assert evidence.body_hash == hashlib.sha256(_TEXT.encode()).digest()
        assert evidence.subject is CompanySubject.ISSUER
        assert evidence.quote in _TEXT
    assert (actual.evidence[0].start_line, actual.evidence[0].end_line) == (3, 3)
    assert (purpose.evidence[0].start_line, purpose.evidence[0].end_line) == (4, 4)


@pytest.mark.parametrize(("title", "statement", "implemented"), (
    ("关于首次回购公司股份的公告", "本公司首次回购股份100万股，拟回购股份占公司总股本的比例为0.3%。", True),
    ("控股股东增持股份公告", "本公司控股股东已增持股份100万股，实际比例详见附表。", True),
    ("关于首次回购公司股份的公告", "本公司拟首次回购股份，占公司总股本的比例为0.3%。", False),
    ("关于首次回购公司股份的公告", "本公司尚未实际首次回购股份，占公司总股本的比例为0.3%。", False),
    pytest.param(
        "关于首次回购公司股份的公告",
        "本公司首次通过股份回购专用证券账户以集中竞价方式回购公司股份100万股，实际比例详见附表。",
        True,
        id="account_repurchase_missing_ratio",
    ),
    pytest.param(
        "控股股东增持股份公告",
        "本公司收到公司控股股东甲方控股有限公司（以下简称“甲方控股”）声明，"
        "甲方控股通过交易系统，以集中竞价方式累计增持本公司股份100万股，实际比例详见附表。",
        True,
        id="holder_alias_missing_ratio",
    ),
))
def test_actual_action_without_a_ratio_publishes_an_applicable_gap_only_when_implemented(
    tmp_path, title, statement, implemented,
):
    index = fixtures._index(tmp_path)
    document = _put(index, statement + _DETAIL * 2, title=title)
    index.publish(document.disclosure.codes, _AT)
    reader = _reader(index)
    before = reader.read(document.disclosure.codes)
    event, = before[0].positive_events
    actual, = (item for item in reader.read_fields(document.disclosure.codes).results if item.field == "actual_pct")

    assert event.status is (CompanyPositiveStatus.IMPLEMENTING if implemented else CompanyPositiveStatus.PLANNED)
    assert event.measurements.actual_ratio_pct is None
    assert event.positive_level == 0 and event.level_milestones == ()
    assert actual.value is None and actual.evidence == ()
    assert actual.applicable is implemented
    assert actual.state == ("verification_required" if implemented else "not_applicable")
    assert actual.reason == ("measurement_missing" if implemented else "action_not_implemented")
    assert reader.read(document.disclosure.codes) == before


def test_same_numeric_value_under_a_changed_subject_cannot_reuse_the_old_proof(tmp_path):
    index = fixtures._index(tmp_path)
    document = _put(index)
    index.publish(document.disclosure.codes, _AT)
    previous, = _reader(index).read(document.disclosure.codes)
    event, = previous.positive_events
    changed = replace(previous, positive_events=(replace(event, subject_type=CompanySubject.CONTROLLING_SHAREHOLDER),))
    prepared = _prepare(index, changed, (document,))
    assert all(item.state == "present_unverified" and not item.evidence for item in prepared.results)
    writer = SQLiteCompanyFactsIndexFixture(index.facts_path)
    writer.publish(
        (changed,), {document.disclosure.identity: "cninfo"}, _identity(index), _AT,
        field_results=prepared.results, receipts=prepared.receipts,
    )
    readback = _reader(index).read_fields(document.disclosure.codes)
    assert all(item.state == "present_unverified" and not item.evidence for item in readback.results)
    actual = next(item for item in readback.results if item.field == "actual_pct")
    assert actual.value == event.measurements.actual_ratio_pct


def test_field_only_publication_advances_generation_without_rewriting_fact_values_or_ordinary_valid(tmp_path):
    index, document, facts = _unproved_publication(tmp_path)
    parent = _identity(index)
    ordinary = _receipts(index, ordinary_only=True)
    before = _reader(index).read(document.disclosure.codes)
    prepared = _prepare(index, facts, (document,))
    assert all(item.state == "verified" for item in prepared.results)
    writer = SQLiteCompanyFactsIndexFixture(index.facts_path)
    writer.publish((), {}, parent, _AT, field_results=prepared.results, receipts=prepared.receipts)
    snapshot = _reader(index).read_fields(document.disclosure.codes)
    assert snapshot.publication_hash != parent.content_hash.hex()
    assert all(item.state == "verified" for item in snapshot.results)
    assert _reader(index).read(document.disclosure.codes) == before
    assert _receipts(index, ordinary_only=True) == ordinary


@pytest.mark.parametrize("failure", ("final_identity", "parent_conflict"))
def test_failed_publication_rolls_back_facts_field_conclusions_and_new_receipts(tmp_path, monkeypatch, failure):
    index, document, facts = _unproved_publication(tmp_path)
    writer = SQLiteCompanyFactsIndexFixture(index.facts_path)
    prepared = _prepare(index, facts, (document,))
    parent = _identity(index)
    if failure == "parent_conflict":
        writer.publish((), {}, parent, _AT, field_results=prepared.results, receipts=prepared.receipts)
    else:
        write_identity = company_facts_index._write_identity

        def reject_after_writes(*args):
            write_identity(*args)
            raise RuntimeError("fixture_final_identity_rejected")

        monkeypatch.setattr(company_facts_index, "_write_identity", reject_after_writes)
    before = _logical_state(index)
    changed = replace(facts, checked_at=_AT + timedelta(seconds=1), published_at=_AT + timedelta(seconds=1))
    receipt = CompanyInputReceipt(
        "cninfo", "new-field-attempt", facts.code, "new-field-version", b"f" * 32, b"b" * 32,
        _AT, "field_attempted", "fixture-field-attempt:1",
    )
    with pytest.raises(RuntimeError, match="fixture_final_identity_rejected|company_publication_parent_changed"):
        writer.publish(
            (changed,), {document.disclosure.identity: "cninfo"}, parent, _AT,
            field_results=prepared.results, receipts=(*prepared.receipts, receipt),
        )
    assert _logical_state(index) == before


def test_failed_same_body_proof_is_attempted_once_and_restart_does_not_queue_a_redownload(tmp_path, monkeypatch):
    index = fixtures._index(tmp_path)
    sentence = "首次回购股份占公司总股本比例为0.3%。"
    document = _put(index, "本公司" + sentence + sentence + _DETAIL)
    calls = []
    extract = company_field_publication.ExtractCompanyFieldEvidence

    def observed(*args):
        calls.append(args[0].disclosure.identity)
        return extract(*args)

    monkeypatch.setattr(company_field_publication, "ExtractCompanyFieldEvidence", observed)
    index.publish(document.disclosure.codes, _AT)
    assert calls == [document.disclosure.identity]
    snapshot = _reader(index).read_fields(document.disclosure.codes)
    actual = next(item for item in snapshot.results if item.field == "actual_pct")
    assert actual.state == "present_unverified" and not actual.evidence
    original_receipts = _receipts(index)
    assert any(row[-1].startswith("field_source_lines:") for row in original_receipts)
    for turn in (1, 2):
        restarted = fixtures._index(tmp_path)
        restarted.prepare_publication(document.disclosure.codes, _AT + timedelta(minutes=turn))
        assert restarted.pending_documents(
            document.disclosure.codes, _AT, policies=fixtures._TEST_DOCUMENT_POLICIES,
        ) == ()
        assert _receipts(restarted) == original_receipts
    assert calls == [document.disclosure.identity]


@pytest.mark.parametrize("mismatch", ("body", "title", "kind", "checked", "code", "time", "url", "terminal"))
def test_wrong_provided_input_cannot_acknowledge_the_current_body_or_suppress_its_next_valid_attempt(tmp_path, mismatch):
    index, document, facts = _unproved_publication(tmp_path)
    before = _receipts(index)
    if mismatch == "body":
        wrong = replace(document, text=document.text.replace("0.3%", "0.9%"))
    elif mismatch == "title":
        wrong = replace(document, disclosure=replace(document.disclosure, title="普通公告"))
    elif mismatch == "kind":
        wrong = replace(document, disclosure=replace(document.disclosure, kind="news"))
    elif mismatch == "checked":
        wrong = replace(document, checked=False)
    elif mismatch == "code":
        wrong = replace(document, disclosure=replace(document.disclosure, codes=("600002",)))
    elif mismatch == "time":
        wrong = replace(document, disclosure=replace(document.disclosure, published_at=_AT + timedelta(seconds=1)))
    elif mismatch == "url":
        wrong = replace(document, disclosure=replace(document.disclosure, url="https://example.test/other.pdf"))
    else:
        wrong = replace(document, terminal_failure=True)
    rejected = _prepare(index, facts, (wrong,))
    assert rejected.receipts == ()
    assert all(item.state == "present_unverified" for item in rejected.results)
    assert _receipts(index) == before
    accepted = _prepare(index, facts, (document,))
    assert len(accepted.receipts) == 2
    assert all(item.state == "verified" for item in accepted.results)
    writer = SQLiteCompanyFactsIndexFixture(index.facts_path)
    writer.publish((), {}, _identity(index), _AT, field_results=accepted.results, receipts=accepted.receipts)
    assert all(item.state == "verified" for item in _reader(index).read_fields(document.disclosure.codes).results)


def test_changed_source_version_reopens_a_previously_failed_proof_attempt(tmp_path, monkeypatch):
    index = fixtures._index(tmp_path)
    sentence = "首次回购股份占公司总股本比例为0.3%。"
    document = _put(index, "本公司" + sentence + sentence + _DETAIL)
    calls = []
    extract = company_field_publication.ExtractCompanyFieldEvidence

    def observed(*args):
        calls.append(args[0].disclosure.digest)
        return extract(*args)

    monkeypatch.setattr(company_field_publication, "ExtractCompanyFieldEvidence", observed)
    index.publish(document.disclosure.codes, _AT)
    before = tuple(row for row in _receipts(index) if row[-1].startswith("field_source_lines:"))
    assert len(before) == 1 and calls == [document.disclosure.digest]
    document = _put(
        index, "本公司" + sentence + _DETAIL + "相关执行情况请以本次正式公告披露为准。",
        identity=document.disclosure.identity, digest="field-record-revised",
    )
    index.publish(document.disclosure.codes, _AT)
    after = tuple(row for row in _receipts(index) if row[-1].startswith("field_source_lines:"))
    assert len(after) == 2 and set(before).issubset(after)
    assert calls == ["field-record", document.disclosure.digest]
    actual = next(item for item in _reader(index).read_fields(document.disclosure.codes).results if item.field == "actual_pct")
    assert actual.value == 0.3
    assert actual.state == "verified"


def test_changed_rule_rejects_an_old_producer_without_committing_an_attempt_and_allows_a_corrected_retry(tmp_path, monkeypatch):
    index, document, facts = _unproved_publication(tmp_path)
    extract = company_field_publication.ExtractCompanyFieldEvidence
    monkeypatch.setattr(company_field_publication, "ExtractCompanyFieldEvidence", lambda *args: ())
    old_attempt = _prepare(index, facts, (document,))
    writer = SQLiteCompanyFactsIndexFixture(index.facts_path)
    writer.publish((), {}, _identity(index), _AT, field_results=old_attempt.results, receipts=old_attempt.receipts)
    old_receipts = _receipts(index)
    ordinary = _receipts(index, ordinary_only=True)
    monkeypatch.setattr(company_field_publication, "ExtractCompanyFieldEvidence", extract)
    rules = tuple(
        (field, "implemented_total_capital_ratio:3" if field == "actual_pct" else rule)
        for field, rule in company_field_publication.COMPANY_FIELD_RULES
    )
    monkeypatch.setattr(company_field_publication, "COMPANY_FIELD_RULES", rules)
    before = _logical_state(index)
    with pytest.raises(ValueError, match="company_field_evidence_rule_mismatch"):
        index.publish(document.disclosure.codes, _AT)
    assert _logical_state(index) == before

    def upgraded(*args):
        return tuple(
            replace(result, evidence=tuple(
                replace(proof, rule_id="implemented_total_capital_ratio:3") for proof in result.evidence
            )) if result.field == "actual_pct" else result
            for result in extract(*args)
        )

    monkeypatch.setattr(company_field_publication, "ExtractCompanyFieldEvidence", upgraded)
    index.publish(document.disclosure.codes, _AT)
    readback = _reader(index).read_fields(document.disclosure.codes)
    actual = next(item for item in readback.results if item.field == "actual_pct")
    assert actual.state == "verified" and actual.evidence[0].rule_id == "implemented_total_capital_ratio:3"
    assert set(old_receipts).issubset(_receipts(index))
    assert _receipts(index, ordinary_only=True) == ordinary
    assert any("implemented_total_capital_ratio:3" in row[-1] for row in _receipts(index))

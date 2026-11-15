"""Normal publication withdraws only an isolated notice's historical measurement."""

from dataclasses import replace
from datetime import timedelta

import pytest

from tests.helpers.download.company import company_evidence as fixtures
from tests.helpers.download.company.company_field_recovery import _facts, _put
from tests.helpers.download.company_resources import CompanyDocumentFilesFixture, SQLiteCompanyEvidenceReaderFixture
from trader.download.domain.company_event_extraction import ExtractCompanyLinkCandidate, COMPANY_REPURCHASE_MAINTENANCE_DIGEST
from trader.download.domain.company_evidence import CompanyDocument
from trader.download.domain.company_events import CompanyPositiveStatus
from trader.download.infra.company_facts_index import ReadCompanyPublicationIdentity
from trader.download.infra.company_field_recovery import CompanyFieldRepairReceipts
from trader.download.infra.company_link_codec import EncodeCompanyLink
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction


_TITLE = "关于注销回购股份、减少注册资本暨修订《公司章程》的公告"
_BODY = (
    "证券代码：600001\n本公司于2022年审议回购方案。截至2023年8月17日，本公司累计回购股份9,522,792股，"
    "占公司总股本的比例为0.82%。回购股份用于注销并减少注册资本。"
    "本公司于本月办理上述已回购股份的注销手续。本公告仅说明已回购股份的后续处置情况。"
)


def _historical_snapshot(tmp_path, *, wrong_header=False):
    index = fixtures._index(tmp_path)
    # The neutral title recreates the former parser's false positive; the
    # stored input has the actual cancellation title before recovery starts.
    item = fixtures._disclosure("old-cancel", "关于回购股份的公告")
    body = _BODY.replace("600001", "600002") if wrong_header else _BODY
    _put(index, item, body)
    index.publish(item.codes, fixtures.NOW)
    facts = _facts(index)
    event, = facts.positive_events
    assert event.measurements.actual_ratio_pct == 0.82 and event.positive_level == 3
    reader = SQLiteCompanyEvidenceReaderFixture(index.facts_path)
    fields = reader.read_fields(item.codes).results
    body_hash = CompanyDocumentFilesFixture(tmp_path / "text").write(body)
    with SqliteSession(index.path) as collected, SqliteTransaction(collected):
        collected.run("UPDATE disclosures SET title=?,body_retained=1,body_hash=?", (_TITLE, body_hash))
        candidate = ExtractCompanyLinkCandidate(CompanyDocument(item, body, True), fixtures.NOW, fixtures.CompanyEventPolicy())
        collected.run("INSERT OR REPLACE INTO document_link_candidates VALUES(?,?,?,?,?)",
                      (item.source, item.identity, item.digest, index._facts.extraction_hash, EncodeCompanyLink(candidate)))
        # Recreate an actual earlier field policy rather than today's attempt.
        collected.run("DELETE FROM company_field_repairs")
        old_rule = "same_body_repurchase_fields:1:" + COMPANY_REPURCHASE_MAINTENANCE_DIGEST.hex()
        for field in ("actual_pct", "repurchase_purpose"):
            collected.run("INSERT INTO company_field_repairs SELECT id,?,record_hash,?,?,?,'pending' FROM disclosures",
                          (facts.code, event.event_id, field, old_rule))
        attempts = CompanyFieldRepairReceipts(collected, item.codes, fixtures.NOW)
        collected.run("UPDATE company_field_repairs SET state='attempted'")
    with SqliteSession(index.facts_path) as formal, SqliteTransaction(formal):
        formal.run("DELETE FROM company_input_receipt WHERE outcome='field_attempted'")
        parent = ReadCompanyPublicationIdentity(formal)
    index._facts.publish((facts,), {item.identity: item.source}, parent, fixtures.NOW, receipts=attempts, field_results=fields)
    return index, replace(item, title=_TITLE)


def _valid(index):
    with SqliteSession(index.facts_path) as formal:
        return tuple(tuple(row) for row in formal.run("SELECT * FROM company_input_receipt WHERE outcome='valid'"))


def test_old_verified_ratio_and_self_derived_rewards_are_withdrawn_once(tmp_path, monkeypatch):
    index, item = _historical_snapshot(tmp_path)
    before, = _facts(index).positive_events
    fields = SQLiteCompanyEvidenceReaderFixture(index.facts_path).read_fields(item.codes).results
    assert next(field for field in fields if field.field == "actual_pct").state == "verified"
    valid = _valid(index)

    index.prepare_publication(item.codes, fixtures.NOW)

    event, = _facts(index).positive_events
    assert event.event_id == before.event_id and event.evidence_ids == before.evidence_ids
    assert event.first_disclosed_at == before.first_disclosed_at and event.last_disclosed_at == before.last_disclosed_at
    assert event.measurements.actual_ratio_pct is None and event.status is CompanyPositiveStatus.PLANNED
    assert event.positive_level == 0 and event.level_milestones == () and event.not_eligible_reason is not None
    actual, = (field for field in SQLiteCompanyEvidenceReaderFixture(index.facts_path).read_fields(item.codes).results
               if field.field == "actual_pct")
    assert actual.value is None and actual.state == "not_applicable" and actual.evidence == ()
    assert _valid(index) == valid
    with SqliteSession(index.facts_path) as formal:
        publication = ReadCompanyPublicationIdentity(formal)
        assert publication.extraction_hash == index._facts.extraction_hash
        receipts = tuple(tuple(row) for row in formal.run("SELECT * FROM company_input_receipt ORDER BY policy_id"))

    def unexpected_read(*args, **kwargs):
        raise AssertionError("committed cancellation correction must not reread its source")

    restarted = fixtures._index(tmp_path)
    monkeypatch.setattr(restarted._documents, "read", unexpected_read)
    restarted.prepare_publication(item.codes, fixtures.NOW)
    assert _facts(restarted).positive_events == (event,) and _valid(restarted) == valid
    with SqliteSession(index.facts_path) as formal:
        assert ReadCompanyPublicationIdentity(formal) == publication
        assert tuple(tuple(row) for row in formal.run("SELECT * FROM company_input_receipt ORDER BY policy_id")) == receipts


@pytest.mark.parametrize("guard", ("wrong_header", "missing_text", "missing_receipt", "shared", "independent"))
def test_missing_material_or_independent_evidence_cannot_be_automatically_rewritten(tmp_path, guard):
    index, item = _historical_snapshot(tmp_path, wrong_header=guard == "wrong_header")
    if guard in {"shared", "independent"}:
        facts = _facts(index)
        event, = facts.positive_events
        if guard == "shared":
            events = (event, replace(event, event_id="another-event"))
        else:
            events = (replace(event, event_id="original-plan", evidence_ids=("original-plan", item.identity),
                              first_disclosed_at=event.first_disclosed_at - timedelta(days=1)),)
        with SqliteSession(index.facts_path) as formal:
            parent = ReadCompanyPublicationIdentity(formal)
        index._facts.publish((replace(facts, positive_events=events),),
                             {key: item.source for event in events for key in event.evidence_ids}, parent, fixtures.NOW)
    if guard == "missing_text":
        def missing(*args, **kwargs):
            raise FileNotFoundError("fixture_missing")
        index._documents.read = missing
    if guard == "missing_receipt":
        with SqliteSession(index.facts_path) as formal, SqliteTransaction(formal):
            formal.run("DELETE FROM company_input_receipt WHERE outcome='valid'")
        index._facts.publish_file()
    before = _facts(index).positive_events
    index.prepare_publication(item.codes, fixtures.NOW)
    assert _facts(index).positive_events == before

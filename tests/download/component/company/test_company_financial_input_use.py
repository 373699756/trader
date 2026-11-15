"""Only a committed exact current report can retire an older report's sole input use."""

from dataclasses import replace
from datetime import date, timedelta
import time

import pytest

from scripts.download.company.company_publication_audit import AuditCompanyPublication
from scripts.download.company.company_query_audit import AuditCompanyDatabase
from tests.helpers.download.company import company_evidence as fixtures
from tests.helpers.download.configuration import CompanyEventPolicyFixture
from tests.helpers.shared.configuration import SqlitePolicyFixture
from trader.download.application.company_evidence import CompanyEvidenceDependencies
from trader.download.domain.company_events import (
    CompanyDataState, CompanyEventStatus, CompanyRiskEvent, CompanyRiskType, CompanySeverityBasis, CompanySubject,
)
from trader.download.domain.company_evidence import CompanyFinancial, DisclosurePage, DisclosurePartition, StructuredCompanyPage, StructuredCompanyQuery
from trader.download.domain.company_financial_report import CompanyFinancialReportRequest
from trader.download.infra import company_financial_input_use
from trader.download.infra.company_fact_inputs import ReadCompanySnapshotRows
from trader.download.infra.company_fact_rows import ReadCompanyFactRows
from trader.download.infra.company_facts_index import ReadCompanyPublicationIdentity
from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession, SqliteTransaction

NOW = fixtures.NOW
CODE = "600001"
OLD_PERIOD = date(2025, 9, 30)
OLD_DISCLOSED = NOW.replace(month=4, day=17, hour=0, minute=0)
NEW_DISCLOSED = NOW.replace(day=10, hour=0, minute=0)
OLD = CompanyFinancial(CODE, OLD_PERIOD, OLD_DISCLOSED, None, 5, None, 3, None, None, 100, 90)
CURRENT = CompanyFinancial(CODE, date(2026, 9, 30), NEW_DISCLOSED, 0, 5, 8, 4, 6, 10, 110, 95)
BODY = "证券代码：600001\n本公司2025年第三季度报告。\n" + "本报告列示本报告期的主要会计数据，相关数据由公司依法披露。" * 5


def _structured(index, report):
    index.commit_structured(
        StructuredCompanyQuery("financial", (NOW - timedelta(days=365)).date(), NOW.date()),
        StructuredCompanyPage("financial", 1, 1, 1, financials=(report,)), NOW,
    )


def _setup(tmp_path, *, publish=True, report=CURRENT):
    index = fixtures._index(tmp_path)
    index.prepare_publication((CODE,), NOW)
    _structured(index, OLD)
    item = fixtures._disclosure("older-current-financial", "2025年第三季度报告", when=OLD_DISCLOSED)
    request = CompanyFinancialReportRequest(CODE, OLD_PERIOD, OLD_DISCLOSED)
    index.commit_financial_reports(request, (item,), NOW)
    index.publish((CODE,), NOW, changed_only=True)
    _structured(index, report)
    if publish:
        index.publish((CODE,), NOW, changed_only=True)
    return index, item


def _pending(index):
    return index.pending_documents((CODE,), NOW, limit=50, policies=fixtures._TEST_DOCUMENT_POLICIES)


def _unused(index, *, cancel_requested=None):
    with (
        SqliteSession(index.path, SqliteConnectionOptions(read_only=True)) as collected, SqliteTransaction(collected),
        SqliteSession(index.facts_path, SqliteConnectionOptions(read_only=True)) as formal, SqliteTransaction(formal),
    ):
        return company_financial_input_use.ReadUnusedCompanyFinancialInputs(
            collected, formal, (CODE,), NOW, index._facts.extraction_hash, cancel_requested=cancel_requested,
        )


def _snapshot(index):
    with (
        SqliteSession(index.path, SqliteConnectionOptions(read_only=True)) as collected, SqliteTransaction(collected),
        SqliteSession(index.facts_path, SqliteConnectionOptions(read_only=True)) as formal, SqliteTransaction(formal),
    ):
        return ReadCompanySnapshotRows(
            collected, formal, (CODE,), NOW, index._facts.extraction_hash, cancel_requested=None,
        )[CODE]


def _risk_reference(index, item):
    with SqliteSession(index.facts_path) as formal:
        fact, = ReadCompanyFactRows(formal, (CODE,))
        parent = ReadCompanyPublicationIdentity(formal)
    risk = CompanyRiskEvent(
        "independent-risk", CompanyRiskType.OFFICIAL_INVESTIGATION, 4, CompanySubject.ISSUER,
        CompanySeverityBasis.HARD, "independent-case", OLD_DISCLOSED, OLD_DISCLOSED, OLD_DISCLOSED,
        CompanyEventStatus.ONGOING, None, None, "发行人立案风险仍有效", (item.identity,),
    )
    index._facts.publish((replace(fact, risk_events=(risk,)),), {item.identity: item.source}, parent, NOW)


class _Sources(fixtures.Sources):
    def __init__(self, item):
        super().__init__()
        self.item = item
        self.document_calls = 0

    def fetch(self, query, *args, **kwargs):
        if isinstance(query, DisclosurePartition):
            present = query.market == "sse" and query.start <= self.item.published_at.date() <= query.end
            return DisclosurePage((self.item,) if present else (), int(present), False)
        return super().fetch(query, *args, **kwargs)

    def document(self, item, *, route="standard"):
        assert item == self.item
        self.document_calls += 1
        return fixtures._extraction(BODY)


def test_normal_sync_skips_only_retired_financial_use_and_restart_keeps_all_consumers_consistent(tmp_path):
    index, item = _setup(tmp_path)
    source = _Sources(item)
    for at in (NOW, NOW + timedelta(minutes=1)):
        result = fixtures._sync_company_evidence(
            CompanyEvidenceDependencies(index, source, source, lambda: 0), (CODE,), at,
        )
        assert result.pending_documents == 0 and source.document_calls == 0
        assert not index.document_required(item, at) and _pending(index) == ()
        workload = index.document_workload((CODE,), at, fixtures._TEST_DOCUMENT_POLICIES)
        assert workload.pending == workload.analyzed_pending == 0
        assert workload.watermarks[0].discovered_day == workload.watermarks[0].processed_day == NOW.date()
        with SqliteSession(index.path) as collected, SqliteSession(index.facts_path) as formal:
            assert collected.run("SELECT COUNT(*) FROM company_document_requirements").require_one()[0] == 1
            assert tuple(collected.run("SELECT body_checked,body_retained FROM disclosures").require_one()) == (0, 0)
            assert formal.run("SELECT COUNT(*) FROM company_input_receipt WHERE source_key=?", (item.identity,)).require_one()[0] == 0
        assert _snapshot(index).disclosures == ()
        audit = AuditCompanyDatabase(index.path, now=at, budget_seconds=30, facts_path=index.facts_path,
                                     event_policy=CompanyEventPolicyFixture(), sqlite_policy=SqlitePolicyFixture())
        assert audit["summary"]["database"]["pending_documents"] == 0
        index = fixtures._index(tmp_path)


def test_new_raw_report_does_not_end_old_use_before_formal_commit(tmp_path):
    index, item = _setup(tmp_path, publish=False)
    assert _pending(index)[0].disclosure == item and index.document_required(item, NOW)
    assert _unused(index) == frozenset()
    index.publish((CODE,), NOW, changed_only=True)
    assert _pending(index) == () and not index.document_required(item, NOW)
    _structured(index, replace(CURRENT, roe_pct=11))
    assert _pending(index)[0].disclosure == item and index.document_required(item, NOW)


def test_retained_unpublished_body_has_no_current_pending_or_watermark_obligation_and_gets_no_valid(tmp_path):
    index, item = _setup(tmp_path)
    assert index.commit_document(item, fixtures._extraction(BODY), NOW)
    with SqliteSession(index.path) as collected, SqliteTransaction(collected):
        collected.run("INSERT INTO coverage VALUES('sse',?,?,?)", ((NOW - timedelta(days=365)).date().isoformat(), NOW.date().isoformat(), NOW.isoformat()))
    workload = index.document_workload((CODE,), NOW, fixtures._TEST_DOCUMENT_POLICIES)
    assert workload.analyzed_pending == 0 and workload.watermarks[0].processed_day == NOW.date()
    index.publish((CODE,), NOW, changed_only=True)
    with SqliteSession(index.path) as collected, SqliteSession(index.facts_path) as formal:
        assert tuple(collected.run("SELECT body_checked,body_retained FROM disclosures").require_one()) == (1, 1)
        assert formal.run("SELECT COUNT(*) FROM company_input_receipt WHERE source_key=?", (item.identity,)).require_one()[0] == 0
    audit = AuditCompanyPublication(index.path, index.facts_path, now=NOW, deadline=time.monotonic() + 30, event_policy=CompanyEventPolicyFixture(),
                                    sqlite_policy=SqlitePolicyFixture())
    assert audit["receipts"]["awaiting_publication_documents"] == 0
    unqualified = AuditCompanyPublication(index.path, index.facts_path, now=NOW, deadline=time.monotonic() + 30,
                                         sqlite_policy=SqlitePolicyFixture())
    assert unqualified["receipts"]["awaiting_publication_documents"] == 1
    with SqliteSession(index.path) as collected:
        collected.run("UPDATE disclosures SET body_retained=0 WHERE source_key=?", (item.identity,))
    assert _unused(index)
    released = AuditCompanyPublication(index.path, index.facts_path, now=NOW, deadline=time.monotonic() + 30,
                                      event_policy=CompanyEventPolicyFixture(), sqlite_policy=SqlitePolicyFixture())
    assert released["receipts"]["released_without_receipts"] == 1
    assert "company_released_body_receipt_missing" in released["findings"]


@pytest.mark.parametrize("kind", ("partial", "retained_complete", "same_period", "raw_null", "missing_publication", "extraction", "late_old", "parent_time", "future"))
def test_incomplete_unbound_late_or_same_period_reports_keep_the_old_input(tmp_path, kind):
    report = CURRENT
    if kind in {"partial", "retained_complete"}:
        report = replace(CURRENT, roe_pct=None)
    elif kind == "same_period":
        report = replace(CURRENT, report_date=OLD_PERIOD)
    index, item = _setup(tmp_path, report=report)
    if kind == "retained_complete":
        _structured(index, replace(OLD, basic_eps=1, revenue_growth_pct=2, core_profit_growth_pct=3, roe_pct=4))
        index.publish((CODE,), NOW, changed_only=True)
        with SqliteSession(index.facts_path) as formal:
            fact, = ReadCompanyFactRows(formal, (CODE,))
            assert fact.financial.report_date == OLD_PERIOD and fact.latest_financial.report_date == CURRENT.report_date
            assert fact.financial_state is CompanyDataState.PARTIAL
    elif kind == "raw_null":
        with SqliteSession(index.path) as collected:
            collected.run("UPDATE financials SET roe_pct=NULL WHERE report_date=?", (CURRENT.report_date.isoformat(),))
    elif kind == "missing_publication":
        with SqliteSession(index.facts_path) as formal:
            formal.run("DELETE FROM company_publication")
    elif kind == "extraction":
        with SqliteSession(index.facts_path) as formal:
            formal.run("UPDATE company_publication SET extraction_hash=?", (b"x" * 32,))
    elif kind == "late_old":
        with SqliteSession(index.path) as collected:
            collected.run("UPDATE disclosures SET published_at=?", ((NEW_DISCLOSED + timedelta(days=1)).isoformat(),))
    elif kind == "parent_time":
        with SqliteSession(index.facts_path) as formal:
            formal.run("UPDATE company_publication SET published_at=published_at-86400000000")
    elif kind == "future":
        with SqliteSession(index.facts_path) as formal:
            formal.run("UPDATE company_publication SET published_at=published_at+86400000000")
    assert _unused(index) == frozenset()
    assert _pending(index)[0].disclosure.identity == item.identity


@pytest.mark.parametrize("kind", ("risk", "shared", "annual", "review_title", "receipt", "dependency", "repair", "candidate", "contract_link", "replacement", "announcement_number", "referenced_candidate", "exclusion"))
def test_independent_and_shared_uses_keep_the_old_material(tmp_path, kind):
    index, item = _setup(tmp_path)
    if kind == "risk":
        _risk_reference(index, item)
    elif kind in {"shared", "annual", "review_title", "dependency", "repair", "candidate", "contract_link", "replacement", "announcement_number", "referenced_candidate"}:
        with SqliteSession(index.path) as collected, SqliteTransaction(collected):
            identifier = collected.run("SELECT id FROM disclosures").require_one()[0]
            if kind == "shared":
                collected.run("INSERT INTO disclosure_codes VALUES(?,?)", ("600002", identifier))
            elif kind == "annual":
                collected.run("UPDATE disclosures SET title='2025年年度报告'")
            elif kind == "review_title":
                collected.run("UPDATE disclosures SET title='2025年第三季度报告及立案调查事项'")
            elif kind == "dependency":
                collected.run("INSERT INTO company_material_dependencies VALUES(?,?,?,?,?,?,?)", (identifier, CODE, item.digest, b"b" * 32, index._facts.extraction_hash, "financial", "field_source_proof_required"))
            elif kind == "repair":
                collected.run("INSERT INTO company_field_repairs VALUES(?,?,?,?,?,?,'pending')", (identifier, CODE, item.digest, "independent-field", "actual_pct", "rule"))
            elif kind in {"candidate", "referenced_candidate"}:
                collected.run("INSERT INTO document_link_candidates VALUES(?,?,?,?,?)", (item.source, item.identity if kind == "candidate" else "another-document", item.digest, index._facts.extraction_hash, '{}' if kind == "candidate" else '{"supersedes":["' + item.identity + '"]}'))
            elif kind == "contract_link":
                collected.run("INSERT INTO document_contract_link_rules VALUES(?,?,?,?,?,?,?,?)", (item.source, item.identity, CODE, item.digest, b"b" * 32, index._facts.extraction_hash, b"p" * 32, "rule"))
            elif kind == "replacement":
                collected.run("INSERT INTO document_supersessions VALUES(?,?,?,?,?,?,?)", (identifier, CODE, item.digest, identifier + 1, "replacement", index._facts.extraction_hash, "official_complete_replacement"))
            else:
                collected.run("INSERT INTO document_announcement_numbers VALUES(?,?,?,?,?,?)", (identifier, item.digest, b"b" * 32, "2025-001", 0, "rule"))
    else:
        with SqliteSession(index.facts_path) as formal:
            if kind == "receipt":
                formal.run("INSERT INTO company_input_receipt VALUES(?,?,?,?,?,?,?,?,?)", (item.source, item.identity, CODE, item.digest, index._facts.extraction_hash, None, 0, "valid", ""))
            else:
                formal.run("INSERT INTO company_exclusion(code,reason,effective_at,source,source_key,evidence_hash) VALUES(?,?,?,?,?,?)", (CODE, 1, 0, 1, item.identity, b"e" * 32))
    assert _unused(index) == frozenset()
    assert _pending(index)[0].disclosure.identity == item.identity


@pytest.mark.parametrize("kind", ("same_period", "risk"))
def test_normal_sync_still_dispatches_same_period_or_independently_referenced_input(tmp_path, kind):
    index, item = _setup(tmp_path, report=replace(CURRENT, report_date=OLD_PERIOD) if kind == "same_period" else CURRENT)
    if kind == "risk":
        _risk_reference(index, item)
    source = _Sources(item)
    fixtures._sync_company_evidence(CompanyEvidenceDependencies(index, source, source, lambda: 0), (CODE,), NOW)
    assert source.document_calls == 1


def test_cancellation_and_publication_identity_change_never_sign_an_input_exit(tmp_path, monkeypatch):
    index, item = _setup(tmp_path)
    with pytest.raises(InterruptedError, match="company_financial_input_use_cancelled"):
        _unused(index, cancel_requested=lambda: True)
    cancelled = False
    has_formal_use = company_financial_input_use._has_formal_use

    def cancel_after_use_checked(*args):
        nonlocal cancelled
        result = has_formal_use(*args)
        cancelled = True
        return result

    with monkeypatch.context() as patch:
        patch.setattr(company_financial_input_use, "_has_formal_use", cancel_after_use_checked)
        with pytest.raises(InterruptedError, match="company_financial_input_use_cancelled"):
            _unused(index, cancel_requested=lambda: cancelled)
    read = company_financial_input_use.ReadCompanyPublicationIdentity
    calls = 0

    def changed(connection):
        nonlocal calls
        calls += 1
        identity = read(connection)
        return identity if calls == 1 else replace(identity, content_hash=b"c" * 32)

    monkeypatch.setattr(company_financial_input_use, "ReadCompanyPublicationIdentity", changed)
    assert _unused(index) == frozenset()
    with SqliteSession(index.path) as collected:
        assert collected.run("SELECT body_checked FROM disclosures WHERE source_key=?", (item.identity,)).require_one()[0] == 0

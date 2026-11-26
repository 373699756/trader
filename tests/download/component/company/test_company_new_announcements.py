"""Current ordinary composition publishes independently verified new official evidence."""

import hashlib
import json
from dataclasses import replace
from datetime import timedelta

import pytest

from tests.download.component.company.test_company_sync_round import _SCOPE
from tests.download.component.company.test_company_sync_runtime import _runtime
from tests.helpers.download.configuration import CompanySyncConfigurationFixture
from tests.helpers.shared.configuration import SqlitePolicyFixture
from trader.download.application.company_financial_producer import BuildCompanyFinancialRoundQuery
from trader.download.application.company_sync import ResumeCompanySyncPendingStep, RunCompanySyncRound
from trader.download.domain.company_evidence import BuildCompanyDocumentPolicies, CompanyDocumentExtraction, CompanyDocumentFailure, CompanyRatio, Disclosure, DisclosurePage, DisclosurePartition, StructuredCompanyPage, StructuredCompanyQuery
from trader.download.domain.company_events import CompanyDataState, CompanyEventStatus, CompanyRiskType
from trader.download.domain.company_sync import CompanySyncBudget, CompanySyncFamily, CompanySyncStepResult
from trader.download.entrypoints.company_evidence_progress import CompanyRoundProgress
from trader.download.entrypoints.sync_progress import SyncProgress
from trader.download.infra.company_disclosure_source import CninfoMarketDisclosureSource
from trader.download.infra.company_announcement_preparation import ReadCompanyAnnouncementCoverage
from trader.download.infra.company_field_publication import CompanyFieldProofRulesHash
from trader.download.infra.company_facts_index import CompanyInputReceipt
from trader.download.infra.company_normalized_index import ReadCompanyNormalizationBaseline
from trader.download.infra.company_raw_files import CompanyRawFiles
from trader.download.infra.company_risk_adoption import ReadCompanyRiskAdoptionWitnesses
from trader.download.infra.company_facts_protocol import RequireCompanyTime
from trader.download.infra.company_sync_journal import SQLiteCompanySyncRoundJournal
from trader.download.infra.company_sync_producers import CompanyAnnouncementProducer
from trader.download.infra.terminal_progress import RenderProgress
from trader.infra.http_contracts import HttpResponse
from trader.infra.http_transport import RequestsHttpTransport
from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.infra.sqlite_connection import SqliteSession
from trader.recommendation.domain.scoring.company import IsCompanyRiskEffective


def _material(runtime, text, *, key="investigation", title="关于收到中国证券监督管理委员会立案告知书的公告", code="600001", days=1, when=None):
    item = Disclosure("cninfo", key, (code,), title, when if when is not None else _SCOPE.cutoff - timedelta(days=days),
        "https://static.cninfo.com.cn/finalpage/2026-11-16/one.PDF", hashlib.sha256((key + text).encode()).hexdigest())
    runtime.material.register_disclosures((item,))
    runtime.material.commit_document(item, CompanyDocumentExtraction(text, "text", b"p" * 32, "test_text"), _SCOPE.cutoff)
    return item


_INVESTIGATION = "证券代码：600001\n证券简称：测试公司\n公告编号：2026-001\n关于收到中国证券监督管理委员会立案告知书的公告\n本公司收到中国证券监督管理委员会出具的《立案告知书》。因本公司涉嫌信息披露违法违规，中国证券监督管理委员会决定对本公司立案调查。本公司将严格按照有关法律法规的规定和要求及时履行信息披露义务。"
_DETAIL = "本公司将依照有关规定持续履行信息披露义务，相关内容请以本次正式公告为准。"
_PLAN = "证券代码：600001 公告编号：2026-101\n关于回购股份的公告\n本公司拟回购股份，回购股份用于注销。" + _DETAIL
_PROGRESS = "证券代码：600001 公告编号：2026-102\n关于回购股份进展的公告\n本公司就公告编号：2026-101所述方案披露实施进展。已实际回购股份占公司总股本的比例为0.2%，回购股份用于注销。" + _DETAIL
_UNLOCK_TITLE = "首次公开发行前股票期权行权限售股上市流通公告"
_UNLOCK = "证券代码：600001 公告编号：2026-045\n本公司董事会保证公告内容真实、准确和完整。\n本次股票上市流通总数为2,034,800股。\n本次股票上市流通日期为2026年11月27日。\n本次上市流通的限售股数量为2,034,800股，股东数量为41人，占截至本公告披露之日公司总股本比例为0.024%。"


def _run(runtime, transport, *, steps=80):
    with transport:
        return runtime.run(_SCOPE.codes, _SCOPE.cutoff, CompanySyncBudget(100, 0, steps, 10),
            cancel_requested=lambda: False, monotonic=lambda: 0)


def test_batched_announcement_inputs_keep_market_window_and_issuer_membership(tmp_path):
    runtime, _, _, _ = _runtime(tmp_path)
    codes = ("000001", "600001", "600002")
    cutoff = _SCOPE.cutoff
    runtime.initialize(codes, cutoff)
    old = _material(runtime, _INVESTIGATION, days=370)
    shared = replace(old, identity="shared", codes=("600001", "600002"), published_at=cutoff, digest="a" * 64)
    unlock = replace(shared, identity="unlock-only", codes=("000001",), title=_UNLOCK_TITLE, digest="b" * 64)
    runtime.material.register_disclosures((shared, unlock))
    with SqliteSession(runtime.material.path, sqlite_policy=SqlitePolicyFixture()) as raw:
        raw.run("INSERT INTO coverage VALUES(?,?,?,?)", ("sse", (cutoff.date() - timedelta(days=365)).isoformat(),
            cutoff.date().isoformat(), cutoff.isoformat()))
        coverage = ReadCompanyAnnouncementCoverage(raw, codes, cutoff, lambda: None)
    assert [coverage[code].pending for code in codes] == [0, 1, 1]
    assert [coverage[code].directory_complete for code in codes] == [False, True, True]
    # An older retained body may still participate in an announcement-number link.
    assert [coverage[code].has_retained_material for code in codes] == [False, True, False]


def test_directory_closure_alone_publishes_window_and_preserves_unverified_initial_risk(tmp_path):
    runtime, reader, _, physical = _runtime(tmp_path)
    cutoff = _SCOPE.cutoff
    start = cutoff.date() - timedelta(days=365)
    runtime.initialize(_SCOPE.codes, cutoff)
    def prepare():
        runtime.prepare_local(_SCOPE, check=lambda: None, cancel_requested=lambda: False, eligible_codes=_SCOPE.codes,
            financial_query=BuildCompanyFinancialRoundQuery(cutoff))
        return reader.read(_SCOPE.codes)[0]

    with SqliteSession(runtime.material.path, sqlite_policy=SqlitePolicyFixture()) as raw:
        raw.run("INSERT INTO coverage VALUES(?,?,?,?)", ("sse", start.isoformat(), (cutoff.date() - timedelta(days=1)).isoformat(), cutoff.isoformat()))
    assert prepare().announcement_coverage_end is None
    with SqliteSession(runtime.material.path, sqlite_policy=SqlitePolicyFixture()) as raw:
        raw.run("INSERT INTO coverage VALUES(?,?,?,?)", ("sse", cutoff.date().isoformat(), cutoff.date().isoformat(), cutoff.isoformat()))
    fact = prepare()
    assert (fact.announcement_coverage_start, fact.announcement_coverage_end) == (start, cutoff.date())
    assert fact.announcement_state is CompanyDataState.PARTIAL
    assert any(reason.reason_code == "initial_risk_state_unverified" for reason in fact.review_reasons)
    assert fact.pending_document_count == 0 and not fact.risk_events and not fact.positive_events
    identity = reader.publication_identity()
    assert prepare() == fact and reader.publication_identity() == identity and physical == []


@pytest.mark.parametrize("scenario", (
    "verified", "source_only", "scoped_denominator", "wrong_issuer", "wrong_date", "wrong_ratio", "zero_ratio",
    "later_revision", "prior_complete", "total_in_body", "restricted_shares", "conflicting_totals", "same_day_date_only",
))
def test_unlock_admits_exact_issuer_event_without_completing_the_window(tmp_path, scenario):
    runtime, reader, transport, physical = _runtime(tmp_path)
    runtime.initialize(_SCOPE.codes, _SCOPE.cutoff)
    observed_at = _SCOPE.cutoff.date()
    day = observed_at + timedelta(days=10)
    observation = CompanyRatio(_SCOPE.codes[0], day, 0 if scenario == "zero_ratio" else 0.0242738391)
    if scenario == "prior_complete":
        prior, = reader.read(_SCOPE.codes)
        prior = replace(prior, unlock_checked_at=_SCOPE.cutoff, unlock_coverage_end=observed_at + timedelta(days=180),
            unlock_state=CompanyDataState.COMPLETE)
        runtime.normalized.publish_prepared(_SCOPE.codes, (prior,), (), (), (), _SCOPE.cutoff,
            field_rules_hash=CompanyFieldProofRulesHash())
    if scenario != "source_only":
        title = _UNLOCK_TITLE
        body = _UNLOCK
        if scenario == "scoped_denominator":
            body = body.replace("公司总股本", "公司流通股本")
        elif scenario == "wrong_issuer":
            body = body.replace("600001", "600002")
        elif scenario == "wrong_date":
            body = body.replace("11月27日", "11月28日")
        elif scenario == "wrong_ratio":
            body = body.replace("0.024%", "0.025%")
        elif scenario in {"total_in_body", "conflicting_totals"}:
            body = body.rsplit("\n", 1)[0] + "\n本次上市流通的限售股股数为2,034,800股，占公司目前股份总数的比例为0.024%。"
            if scenario == "conflicting_totals":
                body += "\n本次上市流通的限售股总数为2,034,801股，占公司总股本比例为0.025%。"
        elif scenario == "restricted_shares":
            title = "关于限制性股票解除限售股份上市流通的提示性公告"
            body = _UNLOCK.split("\n本次股票")[0] + (
                "\n本次解除限售的限制性股票的上市流通日为2026年11月27日。"
                "\n本次解除限售的限制性股票数量为2,034,800股，占目前公司总股本比例为0.024%。"
            )
        when = _SCOPE.cutoff if scenario == "same_day_date_only" else None
        notice = _material(runtime, body, key="unlock", title=title, days=2, when=when)
        if scenario == "later_revision":
            runtime.material.register_disclosures((replace(notice, identity="revision", title=f"{title}的更正公告",
                published_at=_SCOPE.cutoff - timedelta(days=1), digest="a" * 64),))
    before = runtime.field_watermark(_SCOPE, _SCOPE.codes, lambda: None)
    runtime.inputs.accept_ratios(_SCOPE.identity, StructuredCompanyQuery("unlock", observed_at, day),
        StructuredCompanyPage("unlock", 1, 1, 1, ratios=(observation,)), _SCOPE.cutoff)
    assert (runtime.field_watermark(_SCOPE, _SCOPE.codes, lambda: None) != before) is (
        scenario not in {"source_only", "prior_complete", "zero_ratio"}
    )
    verified = scenario in {"verified", "total_in_body", "restricted_shares"}
    if verified:
        _run(runtime, transport)
    else:
        runtime.prepare_local(_SCOPE, check=lambda: None, cancel_requested=lambda: False, eligible_codes=_SCOPE.codes,
            financial_query=BuildCompanyFinancialRoundQuery(_SCOPE.cutoff))
    fact, = reader.read(_SCOPE.codes)
    assert fact.pending_document_count == 0
    assert not fact.risk_events and not fact.positive_events and physical == []
    with SqliteSession(runtime.facts.path) as formal:
        valid = formal.run("SELECT source_key FROM company_input_receipt WHERE outcome='valid'").fetchall()
    if scenario == "prior_complete":
        assert not fact.unlock_schedule and not valid
        assert (fact.unlock_state, fact.unlock_checked_at, fact.unlock_coverage_end) == (
            prior.unlock_state, prior.unlock_checked_at, prior.unlock_coverage_end,
        )
        return
    assert fact.unlock_checked_at is None and fact.unlock_coverage_end is None
    if not verified:
        assert fact.unlock_schedule == () and fact.unlock_state is CompanyDataState.UNAVAILABLE
        assert not valid
        return
    assert fact.unlock_schedule == (replace(observation, ratio_pct=0.024),)
    assert fact.unlock_state is CompanyDataState.PARTIAL
    assert [row[0] for row in valid] == ["unlock"]
    assert any((review.scope, review.reason_code, review.evidence_id) == ("unlock", "window_incomplete", "unlock") for review in fact.review_reasons)
    link, = reader.read_document_links(_SCOPE.codes).links
    assert link.link.evidence_id == "unlock" and link.link.body_checked
    with SqliteSession(runtime.material.path) as storage:
        assert storage.run("SELECT scope,reason_code FROM company_material_dependencies").require_one()[:] == ("unlock", "window_incomplete")
    head = reader.publication_identity()
    runtime._read_document = lambda digest: pytest.fail("proved_unlock_original_must_not_be_read_again")
    _run(runtime, transport)
    assert reader.publication_identity() == head and physical == []

    runtime.material.register_disclosures((replace(
        notice, identity="later-correction", title=f"{_UNLOCK_TITLE}的更正公告",
        published_at=_SCOPE.cutoff - timedelta(days=1), digest="c" * 64,
    ),))
    _run(runtime, transport)
    after_correction, = reader.read(_SCOPE.codes)
    assert after_correction.unlock_schedule == fact.unlock_schedule
    assert after_correction.unlock_state is CompanyDataState.PARTIAL and physical == []


def test_retained_new_original_reaches_normal_formal_reader_without_a_source_budget(tmp_path):
    runtime, reader, transport, physical = _runtime(tmp_path)
    runtime.initialize(_SCOPE.codes, _SCOPE.cutoff)
    item = _material(runtime, _INVESTIGATION)
    with transport:
        result = runtime.run(_SCOPE.codes, _SCOPE.cutoff, CompanySyncBudget(100, 0, 20, 10),
            cancel_requested=lambda: False, monotonic=lambda: 0)
    fact, = reader.read(_SCOPE.codes)
    assert any(event.risk_type is CompanyRiskType.OFFICIAL_INVESTIGATION for event in fact.risk_events)
    assert fact.financial is None and fact.financial_state.value == "unavailable"
    assert fact.announcement_state.value == "partial" and not result.business_complete and physical == []
    with SqliteSession(runtime.facts.path) as connection:
        assert connection.run("SELECT outcome FROM company_input_receipt WHERE source_key=? AND extraction_hash=?",
            (item.identity, runtime.facts.extraction_hash)).require_one()[0] == "valid"


def test_changed_risk_rules_recheck_selected_old_valid_body_once_even_when_still_ambiguous(tmp_path):
    runtime, reader, _, physical = _runtime(tmp_path)
    cutoff = _SCOPE.cutoff
    runtime.initialize(_SCOPE.codes, cutoff)
    held = "证券代码：600001\n关于控股股东被动减持股份的提示性公告\n公司控股股东所持股份被司法冻结，尚未收到法院的卖出裁定。" + _DETAIL
    notice = _material(runtime, held, key="judicial", title="关于控股股东被动减持股份的提示性公告")
    other = _material(runtime, _INVESTIGATION)
    old = tuple(CompanyInputReceipt(item.source, item.identity, item.codes[0], item.digest, runtime.facts.extraction_hash,
        hashlib.sha256(body.encode()).digest(), cutoff - timedelta(hours=1), "valid", "earlier-rules")
        for item, body in ((notice, held), (other, _INVESTIGATION)))
    runtime.normalized.publish_prepared(_SCOPE.codes, reader.read(_SCOPE.codes), (), old, (), cutoff,
        field_rules_hash=CompanyFieldProofRulesHash(), sources=tuple((item.source_key, item.source) for item in old))
    reads = []
    read_document = runtime._read_document
    def read(digest):
        reads.append(digest)
        return read_document(digest)
    runtime._read_document = read
    def prepare():
        runtime.prepare_local(_SCOPE, check=lambda: None, cancel_requested=lambda: False, eligible_codes=_SCOPE.codes,
            financial_query=BuildCompanyFinancialRoundQuery(cutoff))
    prepare()
    fact, = reader.read(_SCOPE.codes)
    assert not fact.risk_events and any(review.evidence_id == "judicial" and review.reason_code == "conclusion_unverified"
        for review in fact.review_reasons)
    assert reads == [old[0].body_hash]
    with SqliteSession(runtime.facts.path) as formal:
        assert formal.run("SELECT COUNT(*) FROM company_input_receipt WHERE policy_id='earlier-rules' AND outcome='valid'").require_one()[0] == 2
        attempt = formal.run("SELECT source_key,body_hash,outcome,policy_id FROM company_input_receipt WHERE policy_id LIKE 'announcement_risk:%'").require_one()
        assert attempt[:] == ("judicial", old[0].body_hash, "field_attempted", "announcement_risk:" + CompanyFieldProofRulesHash().hex())
    assert not physical
    head = reader.publication_identity()
    reads.clear()
    prepare()
    assert not reads and not physical and reader.publication_identity() == head


@pytest.mark.parametrize("old_valid", [False, True])
def test_issuer_risk_adoption_separates_local_time_from_fixed_cutoff_and_recovers_exact_receipt(tmp_path, tmp_path_factory, monkeypatch, old_valid):
    runtime, reader, _, physical = _runtime(tmp_path)
    cutoff = _SCOPE.cutoff
    adopted = cutoff + timedelta(hours=1)
    runtime.facts._adoption_clock = lambda: adopted
    runtime.initialize(_SCOPE.codes, cutoff)
    title = "关于收到北京证监局警示函的公告"
    body = ("证券代码：600001\n证券简称：示例科技\n公告编号：2026-088\n" + title + "。本公司保证信息披露准确。"
        "北京示例科技股份有限公司于近日收到中国证券监督管理委员会北京监管局下发的《关于对北京示例科技股份有限公司、甲某"
        "采取出具警示函措施的决定》（［2026］244号）。根据有关规定，我局决定对你们采取出具警示函的行政监管措施。"
        "特此公告。北京示例科技股份有限公司董事会2026年11月16日")
    notice = _material(runtime, body, key="adoption", title=title)
    if old_valid:
        original = CompanyInputReceipt(notice.source, notice.identity, notice.codes[0], notice.digest, runtime.facts.extraction_hash,
            hashlib.sha256(body.encode()).digest(), cutoff - timedelta(hours=1), "valid", "earlier-rules")
        runtime.normalized.publish_prepared(_SCOPE.codes, reader.read(_SCOPE.codes), (), (original,), (), cutoff,
            field_rules_hash=CompanyFieldProofRulesHash(), sources=((original.source_key, original.source),))
    def prepare():
        return runtime.prepare_local(_SCOPE, check=lambda: None, cancel_requested=lambda: False,
            eligible_codes=_SCOPE.codes, financial_query=BuildCompanyFinancialRoundQuery(cutoff))
    def fail_after_staging(*args, **kwargs):
        raise InterruptedError("staging_committed")
    with monkeypatch.context() as patch:
        patch.setattr(runtime.facts, "publish", fail_after_staging)
        with pytest.raises(InterruptedError, match="staging_committed"):
            prepare()
    assert not reader.read(_SCOPE.codes)[0].risk_events
    formal_at = adopted + timedelta(days=1)
    runtime.facts._adoption_clock = lambda: formal_at
    prepare()
    fact, = reader.read(_SCOPE.codes)
    risk, = fact.risk_events
    assert risk.risk_level == 1 and risk.effective_at == notice.published_at
    assert fact.checked_at == cutoff and fact.published_at == formal_at
    with SqliteSession(runtime.facts.path) as conn:
        adoption, = conn.run("SELECT published_at FROM company_input_receipt WHERE policy_id GLOB 'formal_risk_adoption:*'").fetchall()
        assert RequireCompanyTime(adoption[0]) == formal_at
        receipt_times = {row[0]: RequireCompanyTime(row[1]) for row in conn.run(
            "SELECT outcome,published_at FROM company_input_receipt WHERE source_key='adoption' AND policy_id NOT GLOB 'formal_risk_adoption:*'")}
    assert receipt_times == ({"valid": cutoff - timedelta(hours=1), "field_attempted": cutoff} if old_valid else {"valid": cutoff})
    with SqliteSession(runtime.material.path) as conn:
        assert conn.run("SELECT count(*) FROM company_input_receipt WHERE policy_id GLOB 'formal_risk_adoption:*'").require_one()[0] == 0
    head = reader.publication_identity()
    assert not prepare().changed_codes and head == reader.publication_identity() and not physical
    with SqliteSession(runtime.facts.path) as conn:
        scope = conn.run("SELECT substr(policy_id,length('formal_risk_adoption:')+1) FROM company_input_receipt WHERE policy_id GLOB 'formal_risk_adoption:*'").require_one()[0]
    with SqliteSession(runtime.material.path) as conn:
        conn.run("UPDATE company_round_intent SET confirmed_content_hash=NULL WHERE scope=?", (scope,))
    assert runtime.normalized.recover_publication_seal() and reader.read(_SCOPE.codes)[0] == fact
    _assert_seeded_risk_keeps_original_visibility(runtime, fact, tmp_path_factory.mktemp("seeded-risk"))
    with SqliteSession(runtime.facts.path) as conn:
        conn.run("DELETE FROM company_input_receipt WHERE policy_id GLOB 'formal_risk_adoption:*'")
    runtime.facts.publish_file()
    with pytest.raises(ValueError, match="company_risk_adoption_confirmation_missing"):
        reader.read(_SCOPE.codes)
    with SqliteSession(runtime.material.path) as conn:
        conn.run("UPDATE company_round_intent SET confirmed_content_hash=NULL WHERE scope=?", (scope,))
    with pytest.raises(ValueError, match="company_normalization_confirmation_changed"):
        runtime.normalized.recover_publication_seal()


def _assert_seeded_risk_keeps_original_visibility(runtime, fact, directory):
    from scripts.download.company.company_field_replay import PrepareCompanyNormalNamespace
    from scripts.download.company.company_research_baseline import ReadCompanyResearchBaseline
    from trader.download.infra.company_facts_reader import SQLiteCompanyEvidenceReader
    from trader.infra.settings.data_paths import DataPaths

    policy = SqlitePolicyFixture()
    with SqliteSession(runtime.facts.path, sqlite_policy=policy) as formal:
        captured = ReadCompanyNormalizationBaseline(formal)
    witness, = captured.risk_adoptions
    assert witness.published_at == fact.published_at
    research = ReadCompanyResearchBaseline(runtime.material.path, runtime.facts.path, runtime.material._documents,
        event_policy=runtime._event_policy, sqlite_policy=policy, check=lambda: None, cancel_requested=lambda: False)
    assert research.consumer_facts == (fact,) and research.baseline.risk_adoptions == (witness,)
    for name, baseline in (("research", research.baseline), ("snapshot", captured)):
        copy, reader, _, _ = _runtime(directory / name)
        copy.normalized.initialize()
        copy.normalized.seed(baseline)
        head = reader.publication_identity()
        assert reader.read(_SCOPE.codes) == (fact,)
        copy.normalized.seed(baseline)
        assert reader.publication_identity() == head
        for path, expected in ((copy.facts.path, (witness,)), (copy.material.path, ())):
            with SqliteSession(path, sqlite_policy=policy) as conn:
                assert ReadCompanyRiskAdoptionWitnesses(conn) == expected
        with SqliteSession(copy.material.path, sqlite_policy=policy) as conn:
            conn.run("UPDATE company_normalization_seed SET complete=0 WHERE singleton=1")
        copy.normalized.seed(baseline)
        assert reader.publication_identity() == head and reader.read(_SCOPE.codes) == (fact,)
    target = directory / "namespace"
    report = PrepareCompanyNormalNamespace(original_facts_path=runtime.facts.path, proven_facts_path=runtime.facts.path,
        collection_path=runtime.material.path, documents=(runtime.material._documents,), directory=target,
        event_policy=runtime._event_policy, sqlite_policy=policy, budget_seconds=30)
    assert report["reader_equal"] and report["original_risks_preserved"] and report["network_requests"] == 0
    reader = SQLiteCompanyEvidenceReader(DataPaths(target).company, event_policy=runtime._event_policy, sqlite_policy=policy)
    assert reader.read(_SCOPE.codes) == (fact,)


def test_employee_option_reaches_formal_fields_without_awards_or_repeat_reads(tmp_path):
    runtime, reader, transport, physical = _runtime(tmp_path)
    runtime.initialize(_SCOPE.codes, _SCOPE.cutoff)
    body = "证券代码：600001 公告编号：2026-103\n本公司首次回购公司股份15万股，占公司总股本比例为0.0258%。\n"
    body += "回购用途\n□减少注册资本\n√用于员工持股计划或股权激励\n□用于转换公司可转债\n回购股份用于员工持股计划，后续另行披露。" + _DETAIL
    _material(runtime, body, key="employee", title="关于首次回购公司股份的公告")
    _run(runtime, transport)
    fact, = reader.read(_SCOPE.codes)
    event, = fact.positive_events
    assert event.measurements.purpose.value == "employee_plan"
    assert event.positive_level == 0 and event.level_milestones == ()
    fields = reader.read_fields(_SCOPE.codes).results
    purpose, = (field for field in fields if field.field == "repurchase_purpose")
    assert purpose.state == "verified" and purpose.value == "employee_plan"
    assert purpose.evidence[0].quote == "用于员工持股计划，"
    head = reader.publication_identity()
    runtime._read_document = lambda digest: pytest.fail("unchanged_purpose_must_not_read_body_again")
    _run(runtime, transport)
    assert reader.publication_identity() == head and physical == []


@pytest.mark.parametrize("body", (_INVESTIGATION.replace("600001", "600002"), _INVESTIGATION.replace("证券代码：600001\n", ""),
    _INVESTIGATION.replace("证券代码：600001", "证券代码：600001 证券代码：600002")))
def test_missing_or_other_issuer_header_cannot_publish_risk_or_valid_receipt(tmp_path, body):
    runtime, reader, transport, _ = _runtime(tmp_path)
    runtime.initialize(_SCOPE.codes, _SCOPE.cutoff)
    _material(runtime, body)
    _run(runtime, transport)
    fact, = reader.read(_SCOPE.codes)
    assert fact.risk_events == ()
    assert any(review.reason_code == "subject_unverified" for review in fact.review_reasons)
    with SqliteSession(runtime.facts.path) as connection:
        assert connection.run("SELECT COUNT(*) FROM company_input_receipt WHERE outcome='valid'").require_one()[0] == 0


def test_changed_same_supplier_identity_cannot_clear_previous_risk(tmp_path):
    runtime, reader, transport, _ = _runtime(tmp_path)
    runtime.initialize(_SCOPE.codes, _SCOPE.cutoff)
    _material(runtime, _INVESTIGATION)
    _run(runtime, transport)
    previous, = reader.read(_SCOPE.codes)
    _material(runtime, _INVESTIGATION.replace("决定对本公司立案调查", "决定终止对本公司调查"))
    _run(runtime, transport)
    fact, = reader.read(_SCOPE.codes)
    assert fact.risk_events == previous.risk_events
    assert any(review.reason_code == "revision_pending" for review in fact.review_reasons)


def test_verified_negative_original_is_not_a_clear_risk_baseline(tmp_path):
    runtime, reader, transport, _ = _runtime(tmp_path)
    runtime.initialize(_SCOPE.codes, _SCOPE.cutoff)
    _material(runtime, "证券代码：600001 公告编号：2026-001\n关于立案事项的说明\n本公司未被立案，不涉及立案。" + _DETAIL,
        title="关于立案事项的说明")
    _run(runtime, transport)
    fact, = reader.read(_SCOPE.codes)
    assert not fact.risk_events and fact.announcement_state.value == "partial"
    assert any(review.reason_code == "initial_risk_state_unverified" for review in fact.review_reasons)


def test_changed_positive_original_preserves_value_but_suspends_scoring_eligibility(tmp_path):
    runtime, reader, transport, _ = _runtime(tmp_path)
    runtime.initialize(_SCOPE.codes, _SCOPE.cutoff)
    _material(runtime, _PLAN, key="plan", title="关于回购股份的公告", days=2)
    _material(runtime, _PROGRESS, key="progress", title="关于回购股份进展的公告")
    _run(runtime, transport)
    previous, = reader.read(_SCOPE.codes)[0].positive_events
    assert previous.positive_level > 0
    _material(runtime, _PROGRESS.replace("0.2%", "0.3%"), key="progress", title="关于回购股份进展的公告")
    _run(runtime, transport)
    fact, = reader.read(_SCOPE.codes)
    event, = fact.positive_events
    assert event.event_id == previous.event_id and event.measurements == previous.measurements
    assert event.positive_level == 0 and event.not_eligible_reason == "revision_pending"
    assert any(review.scope == "positive" and review.reason_code == "revision_pending" for review in fact.review_reasons)


def test_verified_closure_keeps_case_and_original_evidence_without_effective_risk(tmp_path):
    runtime, reader, transport, _ = _runtime(tmp_path)
    runtime.initialize(_SCOPE.codes, _SCOPE.cutoff)
    _material(runtime, _INVESTIGATION.replace("《立案告知书》", "《立案告知书〔2026〕调查1号》"), days=2)
    _run(runtime, transport)
    _material(runtime, "证券代码：600001 公告编号：2026-002\n关于调查终结的公告\n本公司收到证监会〔2026〕调查1号通知，调查终结。" + _DETAIL,
        key="closure", title="关于调查终结的公告")
    _run(runtime, transport)
    fact, = reader.read(_SCOPE.codes)
    event, = fact.risk_events
    assert event.status is CompanyEventStatus.RESOLVED and event.evidence_ids == ("investigation", "closure")
    assert not IsCompanyRiskEffective(event, _SCOPE.cutoff)
    assert any(review.reason_code == "initial_risk_state_unverified" for review in fact.review_reasons)


@pytest.mark.parametrize("progress_first", (False, True))
def test_number_bound_progress_publishes_field_even_if_parent_arrives_later(tmp_path, progress_first):
    runtime, reader, transport, physical = _runtime(tmp_path)
    runtime.initialize(_SCOPE.codes, _SCOPE.cutoff)
    if progress_first:
        _material(runtime, _PROGRESS, key="progress", title="关于回购股份进展的公告")
        _run(runtime, transport)
        assert any(review.reason_code == "progress_unlinked" for review in reader.read(_SCOPE.codes)[0].review_reasons)
    _material(runtime, _PLAN, key="plan", title="关于回购股份的公告", days=2)
    _run(runtime, transport)
    if not progress_first:
        _material(runtime, _PROGRESS, key="progress", title="关于回购股份进展的公告")
    _run(runtime, transport)
    fact, = reader.read(_SCOPE.codes)
    event, = fact.positive_events
    assert event.evidence_ids == ("plan", "progress") and event.measurements.actual_ratio_pct == 0.2
    assert not any(review.reason_code == "progress_unlinked" for review in fact.review_reasons)
    fields = reader.read_fields(_SCOPE.codes).results
    assert any(field.field == "actual_pct" and field.state == "verified" and field.value == 0.2 for field in fields)
    head = reader.publication_identity()
    runtime._read_document = lambda digest: pytest.fail("unchanged_round_must_not_read_bodies")
    _run(runtime, transport)
    assert reader.publication_identity() == head and physical == []


def test_more_than_one_local_batch_converges_without_skipping_retained_documents(tmp_path):
    runtime, reader, transport, _ = _runtime(tmp_path)
    runtime.initialize(_SCOPE.codes, _SCOPE.cutoff)
    for number in range(17):
        _material(runtime, _INVESTIGATION.replace("2026-001", f"2026-{number + 1:03}"), key=f"case-{number}")
    _run(runtime, transport)
    with SqliteSession(runtime.facts.path) as connection:
        assert connection.run("SELECT COUNT(*) FROM company_input_receipt WHERE outcome='valid'").require_one()[0] == 17
    assert reader.read(_SCOPE.codes)[0].pending_document_count == 0


@pytest.mark.parametrize("failure", (False, True))
def test_new_original_dispatch_uses_one_attempt_and_continues_after_parser_failure(tmp_path, failure):
    runtime, _, _, _ = _runtime(tmp_path)
    runtime.initialize(_SCOPE.codes, _SCOPE.cutoff)
    item = Disclosure("cninfo", "new", _SCOPE.codes, "关于收到立案告知书的公告", _SCOPE.cutoff - timedelta(days=1), "https://example.test/new.PDF", "a" * 64)
    runtime.material.register_disclosures((item,))
    calls = []

    class Source:
        def document_ready(self, disclosure):
            return False

        def document_payload_hash(self, disclosure):
            return None

        def download_document(self, disclosure, *, attempt_limit):
            calls.append((disclosure.identity, attempt_limit))

        def extract_document(self, disclosure, **kwargs):
            if failure:
                raise CompanyDocumentFailure("disclosure_document_text_empty")
            return CompanyDocumentExtraction(_INVESTIGATION, "text", b"p" * 32, "test_text")

    config = CompanySyncConfigurationFixture()
    producer = CompanyAnnouncementProducer(runtime.inputs, runtime.material, Source(), _SCOPE,
        sqlite_policy=SqlitePolicyFixture(), extraction_hash=runtime.facts.extraction_hash, configuration=config,
        policies=BuildCompanyDocumentPolicies(config, "ocr-test", text_identity="text-test"), wall_clock=lambda: _SCOPE.cutoff)
    producer.advance(_SCOPE, cancel_requested=lambda: False, deadline=100)
    state = producer.state(_SCOPE)
    assert state.source_required
    result = producer.advance(_SCOPE, cancel_requested=lambda: False, deadline=100)
    assert calls == [("new", 1)] and result.can_continue and result.source_calls == 1
    assert ("announcement_document_failed" in result.gaps) == failure
    with SqliteSession(runtime.material.path) as connection:
        row = connection.run("SELECT body_checked,body_retained FROM disclosures WHERE source_key='new'").require_one()
        assert tuple(row) == ((0, 0) if failure else (1, 1))


@pytest.mark.parametrize(("prior_check", "denied"), [(False, False), (True, False), (False, True)])
def test_latest_announcements_cover_increment_fetch_current_and_preserve_year_cursor(tmp_path, prior_check, denied):
    from trader.download.domain.company_evidence import CompanySourceFailure

    runtime, _, _, _ = _runtime(tmp_path)
    scope = _SCOPE
    runtime.initialize(scope.codes, scope.cutoff)
    today = scope.cutoff.date()
    prior = scope.cutoff - timedelta(days=2)
    historical_day = today - timedelta(days=340)
    historical_backfill = DisclosurePartition("sse", historical_day, historical_day)
    backfilled_at = prior - timedelta(days=1)
    runtime.material.register_partition(historical_backfill, backfilled_at)
    runtime.material.commit_page(historical_backfill, DisclosurePage((), 0, False), backfilled_at)
    if prior_check:
        older = scope.cutoff - timedelta(days=100)
        unrelated = DisclosurePartition("sse", older.date(), older.date())
        runtime.material.register_partition(unrelated, older)
        runtime.material.commit_page(unrelated, DisclosurePage((), 0, False), older)
        partition = DisclosurePartition("sse", prior.date(), prior.date())
        runtime.material.register_partition(partition, prior)
        runtime.material.commit_page(partition, DisclosurePage((), 0, False), prior)
    current = Disclosure("cninfo", "current-notice", scope.codes, "关于收到立案告知书的公告", scope.cutoff,
        "https://example.test/current.pdf", "a" * 64)
    historical = replace(current, identity="old-notice", published_at=scope.cutoff - timedelta(days=90))
    revised = replace(historical, identity="known-revised", digest="c" * 64)
    runtime.material.register_disclosures((historical, revised))
    with SqliteSession(runtime.material.path, sqlite_policy=SqlitePolicyFixture()) as raw:
        raw.run("""INSERT INTO company_material_dependencies SELECT id,?, ?,?,?, 'risk','risk_material_missing'
            FROM disclosures WHERE source_key=?""",
            (scope.codes[0], "b" * 64, b"x" * 32, runtime.facts.extraction_hash, revised.identity))
    pages, downloads = [], []

    class Source:
        def fetch(self, partition, *, order):
            pages.append((partition.start, partition.end))
            docs = (current,) if partition.end == today else ()
            return DisclosurePage(docs, len(docs), False)

        def document_ready(self, item):
            return False

        def document_payload_hash(self, item):
            return None

        def download_document(self, item, *, attempt_limit):
            downloads.append(item.identity)
            if denied and item.identity == revised.identity:
                raise CompanySourceFailure("access_denied", status_code=403)

        def extract_document(self, item, **kwargs):
            return CompanyDocumentExtraction(_INVESTIGATION, "text", b"p" * 32, "test_text")

    config = CompanySyncConfigurationFixture()
    def producer(latest):
        return CompanyAnnouncementProducer(runtime.inputs, runtime.material, Source(), scope, sqlite_policy=SqlitePolicyFixture(),
            extraction_hash=runtime.facts.extraction_hash, configuration=config,
            policies=BuildCompanyDocumentPolicies(config, "ocr-test", text_identity="text-test"),
            wall_clock=lambda: scope.cutoff, latest_only=latest)

    latest = producer(True)
    full = producer(False)
    assert latest.plan == full.plan
    latest.prepare_latest_coverage(scope)
    if prior_check:
        assert runtime.material.pending_partitions(unrelated) == ()
        assert runtime.material.pending_partitions(partition) == (partition,)
        with SqliteSession(runtime.material.path) as raw:
            assert raw.run("SELECT checked_at FROM coverage WHERE market='sse' AND end_date=?",
                           (unrelated.end.isoformat(),)).require_one()[0] == older.isoformat()
    for _ in range(12):
        if latest.latest_inputs_settled(scope):
            break
        latest.advance(scope, cancel_requested=lambda: False, deadline=100)
        latest = producer(True)
        latest.freeze_latest_window(scope)
    assert latest.latest_inputs_settled(scope) and latest.latest_inputs_complete(scope) is (not denied)
    assert sorted(downloads) == sorted((revised.identity, current.identity))
    if denied:
        result = latest.advance(scope, cancel_requested=lambda: False, deadline=100)
        assert result.deferred and result.gaps == ("latest_announcement_original_unverified",)
    first = prior.date() if prior_check else today
    covered = {a + timedelta(days=i) for a, b in pages for i in range((b - a).days + 1)}
    assert covered == {first + timedelta(days=i) for i in range((today - first).days + 1)}
    following = replace(scope, cutoff=scope.cutoff + timedelta(days=3))
    # A subsequent historical reconciliation can replace the provisional coverage timestamp.
    with SqliteSession(runtime.material.path, sqlite_policy=SqlitePolicyFixture()) as raw:
        raw.run("UPDATE coverage SET checked_at=? WHERE market='sse' AND end_date=?",
                ((scope.cutoff + timedelta(days=1)).isoformat(), today.isoformat()))
    assert runtime.inputs.freeze_latest_announcement_windows(following.identity, following.cutoff, ("sse",)) == (
        DisclosurePartition("sse", today, following.cutoff.date()),)
    full.advance(scope, cancel_requested=lambda: False, deadline=100)
    assert runtime.material.pending_partitions(DisclosurePartition("sse", today, today)) == ()
    assert runtime.material.pending_partitions(DisclosurePartition("sse", today - timedelta(days=365), today))


def test_explicit_annual_backfill_after_latest_resumes_the_old_left_edge_page(tmp_path):
    runtime, _, _, _ = _runtime(tmp_path)
    old = _SCOPE
    scope = replace(old, cutoff=old.cutoff + timedelta(days=1))
    runtime.initialize(old.codes, old.cutoff)
    material = runtime.material
    edge = DisclosurePartition("sse", old.cutoff.date() - timedelta(days=365),
                               old.cutoff.date() - timedelta(days=1))
    row = Disclosure("cninfo", "left-first", old.codes, "公告", old.cutoff - timedelta(days=2),
                     "https://example.test/left.pdf", "a" * 64)
    material.register_partition(edge, old.cutoff)
    material.commit_page(edge, DisclosurePage((row,), 2, True), old.cutoff)
    fetched = []

    class Source:
        def fetch(self, partition, *, order):
            fetched.append(partition)
            if partition.start == edge.start:
                return DisclosurePage((replace(row, identity="left-second", digest="b" * 64),), 2, False)
            return DisclosurePage((), 0, False)

    def producer(latest):
        return CompanyAnnouncementProducer(runtime.inputs, material, Source(), scope, sqlite_policy=SqlitePolicyFixture(),
            extraction_hash=runtime.facts.extraction_hash, latest_only=latest)
    latest = producer(True)
    for _ in range(5):
        if latest.latest_inputs_complete(scope):
            break
        latest.advance(scope, cancel_requested=lambda: False, deadline=100)
    assert latest.latest_inputs_complete(scope) and all(item.start == scope.cutoff.date() for item in fetched)
    full = producer(False)
    for _ in range(5):
        full.advance(scope, cancel_requested=lambda: False, deadline=100)
        if fetched[-1].start == edge.start:
            break
    assert fetched[-1] == replace(edge, page=2, expected_rows=2, received_rows=1)
    with SqliteSession(material.path) as raw:
        assert raw.run("SELECT received_rows,is_complete FROM partitions WHERE market=? AND start_date=? AND end_date=?",
                       ("sse", edge.start.isoformat(), edge.end.isoformat())).require_one()[:] == (2, 1)


def test_raw_quota_rejection_reports_the_reservation_before_network_dispatch(tmp_path):
    runtime, _, _, _ = _runtime(tmp_path)
    runtime.initialize(_SCOPE.codes, _SCOPE.cutoff)
    item = Disclosure("cninfo", "quota", _SCOPE.codes, "关于收到立案告知书的公告", _SCOPE.cutoff,
        "https://example.test/quota.PDF", "a" * 64)
    runtime.material.register_disclosures((item,))
    raw = CompanyRawFiles(tmp_path / "raw", capacity=1000, analysis_reserve=200, free_reserve=10,
        maximum_document_bytes=1000)
    messages = []
    progress = CompanyRoundProgress(SyncProgress(messages.append, lambda: 0.0), lambda: 0.0)

    class Source:
        def document_ready(self, disclosure):
            return False

        def download_document(self, disclosure, *, attempt_limit):
            with raw.reserve(raw.directory(disclosure), 801):
                pytest.fail("request must not be dispatched")

    config = CompanySyncConfigurationFixture()
    producer = CompanyAnnouncementProducer(runtime.inputs, runtime.material, Source(), _SCOPE,
        sqlite_policy=SqlitePolicyFixture(), extraction_hash=runtime.facts.extraction_hash, configuration=config,
        policies=BuildCompanyDocumentPolicies(config, "ocr-test", text_identity="text-test"),
        wall_clock=lambda: _SCOPE.cutoff, observe_download=lambda event: progress.download(event, None))
    producer.advance(_SCOPE, cancel_requested=lambda: False, deadline=100)
    result = producer.advance(_SCOPE, cancel_requested=lambda: False, deadline=100)
    assert not result.can_continue and result.source_calls == 0
    assert result.gaps == ("company_raw_capacity_unavailable",)
    line = RenderProgress(messages[-1])
    for detail in ("[download_headroom]", "本次申请 801", "原件占用 0/1000", "已预约 0（含分析 0）",
                   "分析预留 200", "磁盘可用", "最低保留 10", "本次材料未接纳"):
        assert detail in line
    assert str(tmp_path) not in line


@pytest.mark.parametrize("failure", ("temporary", "repeated", "unavailable", "directory_timeout"))
def test_same_round_document_and_directory_cooldowns_keep_other_work_and_clear_recovered_errors(tmp_path, failure):
    from trader.download.domain.company_evidence import CompanySourceFailure

    runtime, _, _, _ = _runtime(tmp_path)
    scope = replace(_SCOPE, families=(CompanySyncFamily.ANNOUNCEMENTS,))
    runtime.initialize(scope.codes, scope.cutoff)
    item = Disclosure("cninfo", "retry-doc", scope.codes, "关于收到立案告知书的公告", scope.cutoff - timedelta(days=1),
        "https://example.test/retry.pdf", "a" * 64)
    runtime.material.register_disclosures((item,))
    clock = [0.0]
    downloads, pages = [], []

    class Source:
        def document_payload_hash(self, disclosure):
            return None
        def document_ready(self, disclosure):
            return False
        def download_document(self, disclosure, *, attempt_limit):
            downloads.append(clock[0])
            if failure == "unavailable":
                raise CompanyDocumentFailure("disclosure_document_unavailable")
            if failure == "repeated" and len(downloads) <= 4 or failure == "temporary" and len(downloads) == 1:
                raise CompanySourceFailure("http_transport_error")
        def extract_document(self, disclosure, **kwargs):
            return CompanyDocumentExtraction(_INVESTIGATION, "text", b"p" * 32, "test_text")
        def fetch(self, partition, *, order):
            pages.append((partition, clock[0]))
            if failure == "directory_timeout" and len(pages) == 1:
                raise CompanySourceFailure("timeout")
            rows = (item,) if partition.start <= item.published_at.date() <= partition.end else ()
            return DisclosurePage(rows, len(rows), False)

    config = CompanySyncConfigurationFixture()
    policies = BuildCompanyDocumentPolicies(config, "ocr-test", text_identity="text-test")
    producer = CompanyAnnouncementProducer(runtime.inputs, runtime.material, Source(), scope,
        sqlite_policy=SqlitePolicyFixture(), extraction_hash=runtime.facts.extraction_hash, configuration=config,
        policies=policies, wall_clock=lambda: scope.cutoff + timedelta(seconds=clock[0]))
    def sleep(seconds):
        assert pages and downloads
        clock[0] += seconds
        assert clock[0] <= 5 * config.document_transient_retry_seconds
    result = RunCompanySyncRound(runtime.journal, (producer,), scope, CompanySyncBudget(None, None, None, float("inf")),
        monotonic=lambda: clock[0], cancel_requested=lambda: False, sleeper=sleep, transient_retry_seconds=config.document_transient_retry_seconds)
    family, = result.families
    with SqliteSession(runtime.material.path) as connection:
        checked = connection.run("SELECT body_checked FROM disclosures WHERE source_key=?", (item.identity,)).require_one()[0]
    if failure == "unavailable":
        assert downloads == [0] and clock[0] == 0 and not checked
        assert not family.result.can_continue and family.result.failure == "disclosure_document_unavailable"
    elif failure == "repeated":
        assert downloads == [n * config.document_transient_retry_seconds for n in range(5)] and checked
        assert family.result.failure is None and "announcement_retry_pending" not in family.result.gaps
    else:
        assert checked and family.result.failure is None and "announcement_document_failed" not in family.result.gaps
        assert clock[0] == config.document_transient_retry_seconds
        if failure == "directory_timeout":
            assert len(pages) == 3 and pages[0][0] == pages[2][0] and downloads == [0]
            assert pages[1][0] != pages[0][0] and pages[1][1] == 0 and pages[2][1] == clock[0]
        else:
            assert downloads == [0, config.document_transient_retry_seconds] and all(at == 0 for _, at in pages)


@pytest.mark.parametrize("with_unlock", (False, True))
def test_risk_and_observed_unlock_originals_share_turns_without_starving_directory(tmp_path, with_unlock):
    runtime, _, _, _ = _runtime(tmp_path)
    codes = tuple(f"600{index:03d}" for index in range(1, 130))
    scope = replace(_SCOPE, codes=codes)
    runtime.initialize(scope.codes, scope.cutoff)
    items = (
        Disclosure("cninfo", "positive", (codes[0],), "关于回购股份进展的公告",
            scope.cutoff - timedelta(days=1), "https://example.test/positive.PDF", "a" * 64),
        Disclosure("cninfo", "investigation", (codes[-2],), "关于立案调查及控股股东自愿承诺不减持的公告",
            scope.cutoff - timedelta(days=3), "https://example.test/investigation.PDF", "b" * 64),
        Disclosure("cninfo", "reduction-ended", (codes[-1],), "关于持股5%以上股东提前终止减持计划的公告",
            scope.cutoff - timedelta(days=2), "https://example.test/reduction-ended.PDF", "c" * 64),
        Disclosure("cninfo", "commitment", (codes[0],), "关于控股股东自愿承诺不减持公司股份的公告",
            scope.cutoff - timedelta(days=1), "https://example.test/commitment.PDF", "d" * 64),
        Disclosure("cninfo", "unlock", (codes[1],), _UNLOCK_TITLE,
            scope.cutoff - timedelta(days=4), "https://example.test/unlock.PDF", "e" * 64),
        Disclosure("cninfo", "date-only", (codes[1],), _UNLOCK_TITLE,
            scope.cutoff.replace(hour=0, minute=0, second=0, microsecond=0), "https://example.test/date-only.PDF", "f" * 64),
    )
    runtime.material.register_disclosures(items)
    day = scope.cutoff.date() + timedelta(days=10)
    observed_code = codes[1] if with_unlock else codes[0]
    runtime.inputs.accept_ratios(scope.identity, StructuredCompanyQuery("unlock", scope.cutoff.date(), day),
        StructuredCompanyPage("unlock", 1, 1, 1, ratios=(CompanyRatio(observed_code, day, 12),)), scope.cutoff)
    calls = []

    class Source:
        def document_ready(self, disclosure):
            return False

        def document_payload_hash(self, disclosure):
            return None

        def download_document(self, disclosure, *, attempt_limit):
            assert attempt_limit == 1
            calls.append(disclosure.identity)

        def extract_document(self, disclosure, **kwargs):
            return CompanyDocumentExtraction(_INVESTIGATION, "text", b"p" * 32, "test_text")

        def fetch(self, partition, *, order):
            calls.append(partition)
            rows = tuple(sorted(
                (item for item in items if partition.start <= item.published_at.date() <= partition.end),
                key=lambda item: item.published_at,
                reverse=True,
            ))
            return DisclosurePage(rows, len(rows), False)

    config = CompanySyncConfigurationFixture()
    policies = BuildCompanyDocumentPolicies(config, "ocr-test", text_identity="text-test")
    producer = CompanyAnnouncementProducer(
        runtime.inputs,
        runtime.material,
        Source(),
        scope,
        sqlite_policy=SqlitePolicyFixture(),
        extraction_hash=runtime.facts.extraction_hash,
        configuration=config,
        policies=policies,
        wall_clock=lambda: scope.cutoff,
    )
    producer.advance(scope, cancel_requested=lambda: False, deadline=100)
    assert producer.state(scope).identity == CanonicalDigest((items[2], policies))
    producer.advance(scope, cancel_requested=lambda: False, deadline=100)
    first_partition = producer._partitions[0]
    assert producer.state(scope).identity == CanonicalDigest(first_partition)
    # A fresh producer must honor both the saved directory turn and the next document class.
    producer = CompanyAnnouncementProducer(runtime.inputs, runtime.material, Source(), scope,
        sqlite_policy=SqlitePolicyFixture(), extraction_hash=runtime.facts.extraction_hash,
        configuration=config, policies=policies, wall_clock=lambda: scope.cutoff)
    for _ in range(6 if with_unlock else 5):
        result = producer.advance(scope, cancel_requested=lambda: False, deadline=100)
        assert result.can_continue and result.source_calls == 1
    second_turn = "unlock" if with_unlock else "investigation"
    assert calls == ["reduction-ended", first_partition, second_turn, producer._partitions[1],
        *(["investigation"] if with_unlock else []), "commitment", "positive"]


@pytest.mark.parametrize("has_frozen_dependency", (False, True))
def test_required_original_recovery_only_reads_its_distinct_frozen_issuers(tmp_path, monkeypatch, has_frozen_dependency):
    from trader.download.infra import company_sync_producers

    runtime, _, _, _ = _runtime(tmp_path)
    scope = replace(_SCOPE, codes=("600001", "600002", "600003"))
    runtime.initialize(scope.codes, scope.cutoff)
    codes = ("600004", "600001", "600001") if has_frozen_dependency else ("600004",)
    for index, code in enumerate(codes):
        item = _material(runtime, _INVESTIGATION, code=code, key=f"dependency-{index}")
        with SqliteSession(runtime.material.path) as connection:
            connection.run("""INSERT INTO company_material_dependencies
                SELECT id,?,record_hash,body_hash,?,'risk','conclusion_unverified' FROM disclosures WHERE source_key=?""",
                (code, runtime.facts.extraction_hash, item.identity))
    inspected = []
    real_recovery = company_sync_producers.ReadCompanyFieldRecovery

    def read_recovery(formal, collected, requested, *args, **kwargs):
        inspected.append(requested)
        return real_recovery(formal, collected, requested, *args, **kwargs)

    monkeypatch.setattr(company_sync_producers, "ReadCompanyFieldRecovery", read_recovery)
    configuration = CompanySyncConfigurationFixture()
    producer = CompanyAnnouncementProducer(runtime.inputs, runtime.material, runtime._official_source, scope,
        sqlite_policy=SqlitePolicyFixture(), extraction_hash=runtime.facts.extraction_hash,
        current_codes=("600002",), configuration=configuration, wall_clock=lambda: scope.cutoff,
        policies=BuildCompanyDocumentPolicies(configuration, "ocr-test", text_identity="text-test"))
    assert producer._document(scope) is None
    assert inspected == ([("600001",)] if has_frozen_dependency else [])


def test_supplemental_unlock_original_dispatch_requires_the_same_scope_issuer_observation(tmp_path):
    runtime, reader, transport, physical = _runtime(tmp_path)
    scope = _SCOPE
    runtime.initialize(scope.codes, scope.cutoff)
    notice = Disclosure(
        "cninfo", "supplemental-unlock", scope.codes, _UNLOCK_TITLE, scope.cutoff - timedelta(days=1),
        "https://example.test/unlock.PDF", "b" * 64,
    )
    calls = []

    class Source:
        def document_payload_hash(self, disclosure):
            return None

        def document_ready(self, disclosure):
            return False

        def download_document(self, disclosure, *, attempt_limit):
            assert attempt_limit == 1
            calls.append(disclosure.identity)

        def extract_document(self, disclosure, **kwargs):
            return CompanyDocumentExtraction(_UNLOCK, "text", b"p" * 32, "test_text")

    configuration = CompanySyncConfigurationFixture()
    policies = BuildCompanyDocumentPolicies(configuration, "ocr-test", text_identity="text-test")
    producer = CompanyAnnouncementProducer(
        runtime.inputs, runtime.material, Source(), scope,
        sqlite_policy=SqlitePolicyFixture(), extraction_hash=runtime.facts.extraction_hash,
        configuration=configuration, policies=policies, wall_clock=lambda: scope.cutoff,
    )
    producer.advance(scope, cancel_requested=lambda: False, deadline=100)
    for partition in producer._partitions:
        rows = (notice,) if partition.start <= notice.published_at.date() <= partition.end else ()
        runtime.material.commit_page(partition, DisclosurePage(rows, len(rows), False), scope.cutoff)
    assert not producer.state(scope).source_required

    day = scope.cutoff.date() + timedelta(days=10)
    observation = CompanyRatio(scope.codes[0], day, 0.0242738391)
    runtime.inputs.accept_ratios(
        scope.identity, StructuredCompanyQuery("unlock", scope.cutoff.date(), day),
        StructuredCompanyPage("unlock", 1, 1, 1, ratios=(observation,)), scope.cutoff,
    )
    assert producer.state(scope).identity == CanonicalDigest((notice, policies))
    result = producer.advance(scope, cancel_requested=lambda: False, deadline=100)
    assert result.source_calls == 1 and calls == [notice.identity]

    _run(runtime, transport)
    fact, = reader.read(scope.codes)
    assert fact.unlock_state is CompanyDataState.PARTIAL
    assert fact.unlock_schedule == (replace(observation, ratio_pct=0.024),)
    assert fact.unlock_checked_at is None and fact.unlock_coverage_end is None
    assert physical == []


@pytest.mark.parametrize(
    "terminal,already_accepted,receipt_fault",
    ((False, False, None), (True, False, None), (True, True, None), (True, False, "digest"), (True, False, "missing")),
)
def test_directory_response_recovers_exact_unsettled_slot_without_fetch(
    tmp_path, terminal, already_accepted, receipt_fault,
):
    runtime, reader, fixture_transport, _ = _runtime(tmp_path)
    fixture_transport.close()
    scope = replace(_SCOPE, families=(CompanySyncFamily.ANNOUNCEMENTS,))
    runtime.initialize(scope.codes, scope.cutoff)
    journal = runtime.journal
    policy = SqlitePolicyFixture()
    config = CompanySyncConfigurationFixture()
    physical = []

    def execute(request):
        physical.append(request)
        return HttpResponse(200, json.dumps({
            "totalRecordNum": 1,
            "hasMore": False,
            "announcements": [{
                "announcementId": "recovered",
                "secCode": scope.codes[0],
                "announcementTitle": "普通公告",
                "announcementTime": int(scope.cutoff.timestamp() * 1000),
                "adjunctUrl": "finalpage/2026-11-17/recovered.PDF",
            }],
        }).encode("utf-8"))

    with RequestsHttpTransport(
        authorize=journal.authorize_request, inspect_response=journal.retain_directory_response,
        execute_request=execute, monotonic=lambda: 0,
    ) as transport:
        source = CninfoMarketDisclosureSource(
            transport, monotonic=lambda: 0, sleeper=lambda _: None, timeout_seconds=1,
            document_download_timeout_seconds=1, document_parse_timeout_seconds=1, request_interval_seconds=0,
            document_limits=config.document_limits, extended_document_limits=config.extended_document_limits,
        )
        producer = CompanyAnnouncementProducer(
            runtime.inputs, runtime.material, source, scope, sqlite_policy=policy,
            extraction_hash=runtime.facts.extraction_hash,
        )
        budget = CompanySyncBudget(1, 1, 1, 10)
        round_id = journal.begin(scope, (producer.plan,), max_source_calls=budget.max_source_calls)
        journal.start(round_id, CompanySyncFamily.ANNOUNCEMENTS, producer.state(scope))
        initialized = producer.advance(scope, cancel_requested=lambda: False, deadline=budget.deadline)
        journal.finish(round_id, CompanySyncFamily.ANNOUNCEMENTS, initialized)
        journal.start(round_id, CompanySyncFamily.ANNOUNCEMENTS, producer.state(scope))
        partition = producer._partitions[0]
        page = source.fetch(partition, order="desc")
        if already_accepted:
            runtime.material.commit_page(partition, page, scope.cutoff)
        if terminal:
            journal.finish(round_id, CompanySyncFamily.ANNOUNCEMENTS, CompanySyncStepResult(
                False, False, gaps=("source_attempt_unconfirmed",), failure="source_attempt_unconfirmed",
            ))
        receipt_path = journal.path.with_name(journal.path.name + ".directory-response.json")
        if receipt_fault == "digest":
            receipt_path.write_text(receipt_path.read_text().replace('"content_hash":"', '"content_hash":"x'))
        elif receipt_fault == "missing":
            receipt_path.unlink()

    # The original source now holds a closed transport. Recovery can only use local data.
    restarted = SQLiteCompanySyncRoundJournal(
        journal.path, sqlite_policy=policy, publication_identity=reader.publication_identity,
    )
    if not terminal:
        assert ResumeCompanySyncPendingStep(
            restarted, (producer,), scope, round_id, budget, monotonic=lambda: 0,
            cancel_requested=lambda: False, recover_directory=producer.recover_directory,
        )
    else:
        RunCompanySyncRound(
            restarted, (producer,), scope, budget, monotonic=lambda: 0,
            cancel_requested=lambda: False, recover_directory=producer.recover_directory,
        )
    family = restarted.family(round_id, CompanySyncFamily.ANNOUNCEMENTS)
    assert len(physical) == restarted.source_calls(round_id) == family.source_calls == 1
    if receipt_fault:
        assert family.unknown_attempts == 1 and family.result.failure == "source_attempt_unconfirmed"
        assert runtime.material.pending_partitions(partition) == (partition,)
        return
    assert family.unknown_attempts == 0 and family.result.failure is None
    assert family.accepted_inputs == 1 and "source_attempt_unconfirmed" not in family.result.gaps
    assert all(not gap.startswith("directory_response_recovered:") for gap in family.result.gaps)
    assert runtime.material.pending_partitions(partition) == ()
    assert restarted.directory_response(round_id) is None
    with SqliteSession(restarted.path) as connection:
        assert tuple(connection.run("SELECT attempt,observed,status FROM company_sync_request").require_one()) == (1, 1, 200)
        if terminal:
            history = tuple(connection.run(
                "SELECT failure,gaps FROM company_sync_step WHERE family='announcements' AND step>=2 ORDER BY step",
            ))
            assert history[0][0] == "source_attempt_unconfirmed"
            assert "directory_response_recovered:2" in json.loads(history[1][1])


@pytest.mark.parametrize("guard", ("other_stock", "multi_stock", "future", "expired", "other_source", "ineligible"))
def test_new_original_dispatch_never_crosses_issuer_time_or_source_scope(tmp_path, guard):
    runtime, _, _, _ = _runtime(tmp_path)
    runtime.initialize(_SCOPE.codes, _SCOPE.cutoff)
    codes = ("600002",) if guard == "other_stock" else ("600001", "600002") if guard == "multi_stock" else _SCOPE.codes
    when = _SCOPE.cutoff + timedelta(days=1) if guard == "future" else _SCOPE.cutoff - timedelta(days=366 if guard == "expired" else 1)
    item = Disclosure("issuer_disclosure" if guard == "other_source" else "cninfo", "outside", codes,
        "关于收到立案告知书的公告", when, "https://example.test/outside.PDF", "b" * 64)
    runtime.material.register_disclosures((item,))
    config = CompanySyncConfigurationFixture()

    class Source:
        def document_payload_hash(self, disclosure):
            pytest.fail("out_of_scope_original_must_not_be_selected")

    producer = CompanyAnnouncementProducer(runtime.inputs, runtime.material, Source(), _SCOPE,
        sqlite_policy=SqlitePolicyFixture(), extraction_hash=runtime.facts.extraction_hash, configuration=config,
        current_codes=() if guard == "ineligible" else _SCOPE.codes,
        policies=BuildCompanyDocumentPolicies(config, "ocr-test", text_identity="text-test"), wall_clock=lambda: _SCOPE.cutoff)
    producer.advance(_SCOPE, cancel_requested=lambda: False, deadline=100)
    producer.state(_SCOPE)

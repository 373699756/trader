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
from trader.download.application.company_sync import ResumeCompanySyncPendingStep, RunCompanySyncRound
from trader.download.domain.company_evidence import BuildCompanyDocumentPolicies, CompanyDocumentExtraction, CompanyDocumentFailure, Disclosure, DisclosurePage
from trader.download.domain.company_events import CompanyEventStatus, CompanyRiskType
from trader.download.domain.company_sync import CompanySyncBudget, CompanySyncFamily, CompanySyncStepResult
from trader.download.infra.company_disclosure_source import CninfoMarketDisclosureSource
from trader.download.infra.company_sync_journal import SQLiteCompanySyncRoundJournal
from trader.download.infra.company_sync_producers import CompanyAnnouncementProducer
from trader.infra.http_contracts import HttpResponse
from trader.infra.http_transport import RequestsHttpTransport
from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.infra.sqlite_connection import SqliteSession
from trader.recommendation.domain.scoring.company import IsCompanyRiskEffective


def _material(runtime, text, *, key="investigation", title="关于收到中国证券监督管理委员会立案告知书的公告", code="600001", days=1):
    item = Disclosure("cninfo", key, (code,), title, _SCOPE.cutoff - timedelta(days=days),
        "https://static.cninfo.com.cn/finalpage/2026-11-16/one.PDF", hashlib.sha256((key + text).encode()).hexdigest())
    runtime.material.register_disclosures((item,))
    runtime.material.commit_document(item, CompanyDocumentExtraction(text, "text", b"p" * 32, "test_text"), _SCOPE.cutoff)
    return item


_INVESTIGATION = "证券代码：600001\n证券简称：测试公司\n公告编号：2026-001\n关于收到中国证券监督管理委员会立案告知书的公告\n本公司收到中国证券监督管理委员会出具的《立案告知书》。因本公司涉嫌信息披露违法违规，中国证券监督管理委员会决定对本公司立案调查。本公司将严格按照有关法律法规的规定和要求及时履行信息披露义务。"
_DETAIL = "本公司将依照有关规定持续履行信息披露义务，相关内容请以本次正式公告为准。"
_PLAN = "证券代码：600001 公告编号：2026-101\n关于回购股份的公告\n本公司拟回购股份，回购股份用于注销。" + _DETAIL
_PROGRESS = "证券代码：600001 公告编号：2026-102\n关于回购股份进展的公告\n本公司就公告编号：2026-101所述方案披露实施进展。已实际回购股份占公司总股本的比例为0.2%，回购股份用于注销。" + _DETAIL


def _run(runtime, transport, *, steps=80):
    with transport:
        return runtime.run(_SCOPE.codes, _SCOPE.cutoff, CompanySyncBudget(100, 0, steps, 10),
            cancel_requested=lambda: False, monotonic=lambda: 0)


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


def test_risk_originals_across_the_population_precede_positive_material_without_starving_directory(tmp_path):
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
    )
    runtime.material.register_disclosures(items)
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
    for _ in range(5):
        result = producer.advance(scope, cancel_requested=lambda: False, deadline=100)
        assert result.can_continue and result.source_calls == 1
    assert calls == [
        "reduction-ended", first_partition, "investigation", producer._partitions[1], "commitment", "positive",
    ]


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

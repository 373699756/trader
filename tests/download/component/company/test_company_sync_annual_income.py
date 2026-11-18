"""New contract dependencies reach the ordinary publisher without repeating source periods."""

from dataclasses import replace
from datetime import date, timedelta

import pytest

from tests.download.component.company.test_company_sync_runtime import _runtime
from tests.download.component.company.test_company_sync_round import _SCOPE
from tests.download.component.company.test_company_new_announcements import _material, _run
from tests.helpers.download.company.company_annual_income import _financial, _income
from trader.download.domain.company_annual_income import CompanyAnnualIncomeRequest, PlanCompanyAnnualIncome, SelectCompanyAnnualIncome
from trader.download.domain.company_events import CompanyDataState, CompanyPositiveStatus, CompanySubject
from trader.download.domain.company_evidence import CompanyInputValidationFailure
from trader.download.domain.company_facts import CompanyFacts
from trader.download.domain.company_sync import CompanySyncBudget, CompanySyncFamily, CompanySourceCancelled, CompanySyncPhasePlan, CompanySyncRoundIdentity
from trader.download.infra.company_facts_index import CompanyPublicationIdentity
from trader.download.infra.company_field_publication import CompanyFieldProofRulesHash
from trader.download.infra.company_normalized_index import CompanyNormalizationBaseline
from trader.infra.sqlite_connection import SqliteSession


_REVENUE = 200_000_000.0
_DETAIL = "本公司将严格按照有关法律法规及时履行信息披露义务，以上内容均以本次正式公告为准。"


def _seed(tmp_path, *, known=True, older_report=False):
    runtime, reader, transport, physical = _runtime(tmp_path)
    finance = _financial() if known else None
    latest = finance
    if older_report:
        finance, latest = _financial(report_date=date(2024, 12, 31)), _financial(basic_eps=None)
    state = CompanyDataState.PARTIAL if older_report else CompanyDataState.COMPLETE if known else CompanyDataState.UNAVAILABLE
    fact = CompanyFacts("600001", finance, state,
        None, CompanyDataState.UNAVAILABLE, (), None, None, CompanyDataState.UNAVAILABLE, (), (), (), 0,
        None, None, CompanyDataState.UNAVAILABLE, _SCOPE.cutoff, _SCOPE.cutoff, latest_financial=latest)
    baseline = CompanyNormalizationBaseline((fact,), (), (), (),
        CompanyPublicationIdentity(b"b" * 32, runtime.facts.extraction_hash, _SCOPE.cutoff), CompanyFieldProofRulesHash())
    runtime.initialize(_SCOPE.codes, _SCOPE.cutoff, baseline=baseline)
    calls = []
    _attach_source(runtime, transport, calls)
    return runtime, reader, transport, physical, calls


def _attach_source(runtime, transport, calls):
    class Source:
        def annual_income(self, request, *, observed_at):
            calls.append(request)
            transport.get("https://example.test/annual-type", timeout=1)
            transport.get("https://example.test/annual-report", timeout=1)
            return tuple(_income(report_date=day, operating_revenue=_REVENUE, observed_at=observed_at) for day in request.report_dates)

    runtime._annual_source = Source()


def _resume(tmp_path, calls):
    runtime, reader, transport, physical = _runtime(tmp_path)
    runtime.initialize(_SCOPE.codes, _SCOPE.cutoff)
    _attach_source(runtime, transport, calls)
    return runtime, reader, transport, physical


def _contract(runtime, *, key="contract", year=2025):
    body = "证券代码：600001 公告编号：2026-105\n本公司已签订正式合同，合同已生效。合同金额人民币1亿元（不含税）。"
    body += f"本合同的金额与公司{year}年度经审计的营业收入进行比较。" + _DETAIL
    return _material(runtime, body, key=key, title="关于签订重大合同的公告")


def _ordinary(runtime, transport, *, cancelled=lambda: False, maximum=20):
    with transport:
        return runtime.run(_SCOPE.codes, _SCOPE.cutoff, CompanySyncBudget(200, maximum, 80, 100),
            cancel_requested=cancelled, monotonic=lambda: 0)


def test_first_contract_and_annual_value_publish_in_the_same_ordinary_invocation(tmp_path):
    runtime, reader, transport, physical, calls = _seed(tmp_path)
    _contract(runtime)
    first = _ordinary(runtime, transport)
    fact, = reader.read(_SCOPE.codes)
    event, = fact.positive_events
    value = event.measurements
    assert calls == [CompanyAnnualIncomeRequest("600001", (date(2025, 12, 31),))]
    assert value.reference_annual_revenue_cny == _REVENUE and value.reference_disclosed_at == _financial().published_at
    assert value.amount_to_revenue_pct is None and value.amount_basis == "unknown" and event.positive_level == 0
    annual = next(family for family in first.families if family.family is CompanySyncFamily.ANNUAL_INCOME)
    assert annual.source_calls == 2 and annual.result.business_complete
    fields = reader.read_fields(_SCOPE.codes).results
    assert next(field for field in fields if field.field == "annual_revenue").state == "present_unverified"
    with SqliteSession(runtime.facts.path) as formal:
        assert formal.run("SELECT COUNT(*) FROM company_input_receipt WHERE source='eastmoney_annual_income' AND outcome='valid'").require_one()[0] == 1
    head, request_count = reader.publication_identity(), len(physical)
    final = _ordinary(runtime, transport)
    assert final.round_id == first.round_id and len(physical) == request_count and reader.publication_identity() == head
    assert not final.business_complete


def test_new_contract_on_resume_reuses_the_period_without_changing_the_round(tmp_path):
    runtime, reader, transport, physical, calls = _seed(tmp_path)
    _contract(runtime)
    first = _ordinary(runtime, transport)
    count = len(physical)
    _contract(runtime, key="new-contract")
    result = _ordinary(runtime, transport)
    assert result.round_id == first.round_id and len(calls) == 1 and len(physical) == count
    assert len(reader.read(_SCOPE.codes)[0].positive_events) == 2
    assert all(event.measurements.reference_annual_revenue_cny == _REVENUE for event in reader.read(_SCOPE.codes)[0].positive_events)


def test_conflicting_previously_published_annual_version_is_not_used_for_new_contract(tmp_path):
    runtime, reader, transport, _, calls = _seed(tmp_path)
    _contract(runtime)
    first = _ordinary(runtime, transport)
    _contract(runtime, key="new-contract")
    with SqliteSession(runtime.facts.path) as formal:
        row = formal.run("SELECT * FROM company_input_receipt WHERE source='eastmoney_annual_income'").require_one()
        formal.run("""INSERT INTO company_input_receipt VALUES (?,?,?,?,?,?,?,?,?)""",
            (row["source"], row["source_key"], row["code"], "f" * 64, row["extraction_hash"],
                row["body_hash"], row["published_at"], row["outcome"], row["policy_id"]))
    result = _ordinary(runtime, transport)
    assert result.round_id == first.round_id and len(calls) == 1
    events = reader.read(_SCOPE.codes)[0].positive_events
    assert len(events) == 2
    assert sorted(event.measurements.reference_annual_revenue_cny is None for event in events) == [False, True]
    with SqliteSession(runtime.facts.path) as formal:
        assert formal.run("SELECT COUNT(*) FROM company_input_receipt WHERE source='eastmoney_annual_income'").require_one()[0] == 2


@pytest.mark.parametrize("guard", ("no_known_report", "no_reference_year", "cancelled"))
def test_missing_disclosure_year_or_cancelled_event_never_guesses_an_annual_request(tmp_path, guard):
    runtime, reader, transport, _, calls = _seed(tmp_path, known=guard != "no_known_report")
    _contract(runtime)
    _run(runtime, transport)
    fact, = reader.read(_SCOPE.codes)
    event, = fact.positive_events
    if guard == "no_reference_year":
        event = replace(event, measurements=replace(event.measurements, reference_report_date=None))
    elif guard == "cancelled":
        event = replace(event, status=CompanyPositiveStatus.CANCELLED, cancelled_at=_SCOPE.cutoff)
    plan = PlanCompanyAnnualIncome(fact.code, (event,), _SCOPE.cutoff,
        financials=(_financial(),) if guard != "no_known_report" else ())
    assert plan.report_dates == () and not calls
    assert plan.pending_count == (0 if guard == "cancelled" else 2)


@pytest.mark.parametrize("guard", ("later_revision", "foreign_currency", "missing_value", "conflict"))
def test_unusable_annual_source_values_stay_unpublished_and_are_not_refetched(tmp_path, guard):
    runtime, reader, transport, _, calls = _seed(tmp_path)
    _contract(runtime)
    original = runtime._annual_source.annual_income

    def source(request, *, observed_at):
        rows = original(request, observed_at=observed_at)
        if guard == "later_revision":
            return (replace(rows[0], updated_at=_SCOPE.cutoff - timedelta(hours=1)),)
        if guard == "foreign_currency":
            return (replace(rows[0], currency="USD"),)
        if guard == "missing_value":
            return (replace(rows[0], operating_revenue=None),)
        return (*rows, replace(rows[0], operating_revenue=1, record_hash="c" * 64))

    runtime._annual_source.annual_income = source
    first = _ordinary(runtime, transport)
    final = _ordinary(runtime, transport)
    assert len(calls) == 1 and final.round_id == first.round_id
    assert reader.read(_SCOPE.codes)[0].positive_events[0].measurements.reference_annual_revenue_cny is None
    assert not next(item for item in final.families if item.family is CompanySyncFamily.ANNUAL_INCOME).result.business_complete
    with SqliteSession(runtime.facts.path) as formal:
        assert formal.run("SELECT COUNT(*) FROM company_input_receipt WHERE source='eastmoney_annual_income'").require_one()[0] == 0


def test_commit_before_acknowledgement_recovers_without_repeating_source(tmp_path, monkeypatch):
    runtime, reader, transport, physical, calls = _seed(tmp_path)
    _contract(runtime)
    original = runtime.inputs.accept_annual

    def accept(*args):
        original(*args)
        raise RuntimeError("fixture_after_annual_input_commit")

    monkeypatch.setattr(runtime.inputs, "accept_annual", accept)
    with pytest.raises(RuntimeError, match="fixture_after_annual_input_commit"):
        _ordinary(runtime, transport)
    runtime, reader, transport, physical = _resume(tmp_path, calls)
    _ordinary(runtime, transport)
    assert len(calls) == 1 and not any("annual" in url for url in physical)
    assert reader.read(_SCOPE.codes)[0].positive_events[0].measurements.reference_annual_revenue_cny == _REVENUE


def test_cancelled_response_never_publishes_or_reissues_the_unaccepted_request(tmp_path):
    runtime, reader, transport, _, calls = _seed(tmp_path)
    _contract(runtime)
    stop = [False]
    original = runtime._annual_source.annual_income

    def source(request, *, observed_at):
        rows = original(request, observed_at=observed_at)
        stop[0] = True
        return rows

    runtime._annual_source.annual_income = source
    assert _ordinary(runtime, transport, cancelled=lambda: stop[0]).cancelled
    stop[0] = False
    runtime, reader, transport, _ = _resume(tmp_path, calls)
    result = _ordinary(runtime, transport)
    assert len(calls) == 1 and not result.business_complete
    assert reader.read(_SCOPE.codes)[0].positive_events[0].measurements.reference_annual_revenue_cny is None


def test_callback_cancelled_before_first_http_can_resume_the_same_request(tmp_path):
    runtime, reader, transport, _, calls = _seed(tmp_path)
    _contract(runtime)
    stop = [False]

    def source(request, *, observed_at):
        stop[0] = True
        raise CompanySourceCancelled("cancelled_before_annual_http")

    runtime._annual_source.annual_income = source
    assert _ordinary(runtime, transport, cancelled=lambda: stop[0]).cancelled
    stop[0] = False
    runtime, reader, transport, _ = _resume(tmp_path, calls)
    _ordinary(runtime, transport)
    assert len(calls) == 1
    assert reader.read(_SCOPE.codes)[0].positive_events[0].measurements.reference_annual_revenue_cny == _REVENUE


def test_changed_period_combination_requests_only_the_new_explicit_period(tmp_path):
    runtime, reader, transport, _, calls = _seed(tmp_path, older_report=True)
    _contract(runtime)
    first = _ordinary(runtime, transport)
    runtime, reader, transport, _ = _resume(tmp_path, calls)
    _contract(runtime, key="older-reference", year=2024)
    final = _ordinary(runtime, transport)
    assert [request.report_dates for request in calls] == [(date(2025, 12, 31),), (date(2024, 12, 31),)]
    assert final.round_id == first.round_id
    assert all(event.measurements.reference_annual_revenue_cny == _REVENUE for event in reader.read(_SCOPE.codes)[0].positive_events)


@pytest.mark.parametrize("guard", ("cancelled", "other_subject"))
def test_cached_income_does_not_change_cancelled_or_other_subject_contracts(tmp_path, guard):
    runtime, reader, transport, _, _ = _seed(tmp_path)
    _contract(runtime)
    _run(runtime, transport)
    event = reader.read(_SCOPE.codes)[0].positive_events[0]
    if guard == "cancelled":
        event = replace(event, status=CompanyPositiveStatus.CANCELLED, cancelled_at=_SCOPE.cutoff)
    else:
        event = replace(event, subject_type=CompanySubject.CONTROLLING_SHAREHOLDER)
    assert SelectCompanyAnnualIncome("600001", event, (_income(observed_at=_SCOPE.cutoff),), _SCOPE.cutoff,
        financials=(_financial(),)) is None


def test_refreshable_source_protocol_still_binds_the_round_identity():
    annual = CompanySyncPhasePlan(CompanySyncFamily.ANNUAL_INCOME, "a" * 64, refresh_on_input_change=True)
    fields = CompanySyncPhasePlan(CompanySyncFamily.FIELDS, "b" * 64, refresh_on_input_change=True)
    original = CompanySyncRoundIdentity(_SCOPE, (annual, fields))
    assert CompanySyncRoundIdentity(_SCOPE, (replace(annual, request_hash="c" * 64), fields)) != original
    assert CompanySyncRoundIdentity(_SCOPE, (annual, replace(fields, request_hash="d" * 64))) == original


def test_known_source_failure_is_retained_without_retrying_the_same_period(tmp_path):
    runtime, _, transport, _, calls = _seed(tmp_path)
    _contract(runtime)

    def source(request, *, observed_at):
        calls.append(request)
        raise CompanyInputValidationFailure("annual_fixture_rejected")

    runtime._annual_source.annual_income = source
    _ordinary(runtime, transport)
    final = _ordinary(runtime, transport)
    annual = next(item for item in final.families if item.family is CompanySyncFamily.ANNUAL_INCOME)
    assert len(calls) == 1 and annual.result.failure is not None and "annual_income_source_failed" in annual.result.gaps

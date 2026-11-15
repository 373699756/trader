"""Local publications never expand the finite report-source collection plan."""

from dataclasses import replace
from datetime import timedelta

import pytest

from tests.helpers.download.company import company_evidence as fixtures
from tests.helpers.download.company.company_annual_income import (
    _contract, _financial, _read, _Sources, _OPERATING_REVENUE, _REPORTED, _SIGNED,
)
from trader.download.application.company_evidence import (
    CompanyEvidenceDependencies, _AnnouncementRun, _CompanyRunContext, _SyncState, _publish_batch,
)
from trader.download.application.company_operations import CompanyOperations
from trader.download.application.company_report_inputs import (
    CompanyReportInputPlan, CollectCompanyReportInputs, PlanCompanyReportInputs,
)
from trader.download.domain.company_evidence import DisclosurePage, DisclosurePartition, StructuredCompanyPage, StructuredCompanyQuery
from trader.download.domain.company_financial_report import CompanyFinancialReportRequest
from trader.download.infra import company_evidence_index
from trader.download.infra.company_fact_rows import ReadCompanyFactRows
from trader.download.infra.company_facts_index import ReadCompanyPublicationIdentity
from trader.infra.sqlite_connection import SqliteSession


def _state(dependencies):
    return _SyncState(CompanyOperations(None, dependencies.monotonic, ()), 1)


def test_direct_and_directory_deadline_publications_never_plan_or_fetch_reports(tmp_path, monkeypatch):
    index = fixtures._index(tmp_path)
    item = _contract(index)
    source = _Sources(item)
    clock = [60.0]
    dependencies = CompanyEvidenceDependencies(index, source, source, lambda: clock[0], coalesce_publications=True)
    state = _state(dependencies)
    context = _CompanyRunContext(fixtures.NOW, 0, fixtures._TEST_COMPANY_CONFIGURATION, fixtures.NOW.date())
    announcements = _AnnouncementRun(dependencies, state, context)
    announcements.prepare(item.codes)

    def forbidden(*args):
        pytest.fail("a local publication must not plan report-source work")

    monkeypatch.setattr(index, "report_input_plan", forbidden)
    for _ in range(2):
        state.publication_pending_since = 0.0
        announcements._publish_due()
        assert _publish_batch(dependencies, item.codes, fixtures.NOW, 0, state) == 1
    assert source.annual_calls == 0


def test_plan_uses_one_collected_and_formal_read_view(tmp_path, monkeypatch):
    index = fixtures._index(tmp_path)
    item = _contract(index)
    annual = company_evidence_index.PlanCompanyAnnualIncomeRequests
    changed = False

    def change_both_databases_after_first_planner(collected, formal, codes, observed_at):
        nonlocal changed
        requests = annual(collected, formal, codes, observed_at)
        if not changed:
            changed = True
            with SqliteSession(index.path) as writer:
                writer.run("DELETE FROM financials")
            with SqliteSession(index.facts_path) as reader:
                fact, = ReadCompanyFactRows(reader, item.codes)
                parent = ReadCompanyPublicationIdentity(reader)
            index._facts.publish((replace(fact, positive_events=()),), {item.identity: item.source}, parent, fixtures.NOW)
        return requests

    monkeypatch.setattr(company_evidence_index, "PlanCompanyAnnualIncomeRequests", change_both_databases_after_first_planner)
    plan = index.report_input_plan(item.codes, fixtures.NOW)
    assert len(plan.annual_income) == len(plan.financial_reports) == 1
    refreshed = index.report_input_plan(item.codes, fixtures.NOW)
    assert refreshed.annual_income == refreshed.financial_reports == ()


def test_execution_keeps_fixed_time_and_new_requests_wait_for_another_checkpoint(tmp_path, monkeypatch):
    index = fixtures._index(tmp_path)
    item = _contract(index)

    class Source(_Sources):
        report_requests = ()

        def annual_income(self, request, *, observed_at):
            assert observed_at == fixtures.NOW
            return super().annual_income(request, observed_at=observed_at)

        def financial_reports(self, request, *, observed_at):
            assert observed_at == fixtures.NOW
            self.report_requests += (request,)
            return ()

    source = Source(item)
    dependencies = CompanyEvidenceDependencies(index, source, source, lambda: 3600)
    state = _state(dependencies)
    plan = PlanCompanyReportInputs(dependencies, item.codes, fixtures.NOW, state)
    extra = replace(plan.financial_reports[0], disclosed_at=_REPORTED + timedelta(days=1))
    new_plan = replace(plan, financial_reports=(*plan.financial_reports, extra))
    plans = []

    def changed_requirements(codes, observed_at):
        plans.append(observed_at)
        return new_plan

    monkeypatch.setattr(index, "report_input_plan", changed_requirements)
    assert CollectCompanyReportInputs(dependencies, plan, state) > 0
    assert plans == [] and source.report_requests == plan.financial_reports and source.annual_calls == 1
    next_plan = PlanCompanyReportInputs(dependencies, item.codes, fixtures.NOW, state)
    assert plans == [fixtures.NOW] and next_plan.annual_income == () and next_plan.financial_reports == (extra,)
    CollectCompanyReportInputs(dependencies, next_plan, state)
    assert source.report_requests == (*plan.financial_reports, extra) and source.annual_calls == 1


def test_new_contract_is_planned_after_its_batch_while_known_risk_and_loss_are_already_formal(tmp_path):
    index = fixtures._index(tmp_path)
    contract = fixtures._disclosure("new-contract", "关于签订重大合同的公告", when=_SIGNED)
    risk = fixtures._disclosure("new-risk", "关于收到中国证监会立案告知书的公告", when=_SIGNED)
    loss = _financial(parent_net_profit=-1)
    query = StructuredCompanyQuery("financial", _REPORTED.date(), fixtures.NOW.date())
    index.commit_structured(query, StructuredCompanyPage("financial", 1, 1, 1, financials=(loss,)), fixtures.NOW)
    body = "本公司已签订正式合同，合同已生效。合同金额人民币2亿元（不含税）。本合同的金额与公司2025年度经审计的营业收入进行比较。"

    class Source(_Sources):
        report_calls = 0

        def fetch(self, query, **kwargs):
            if isinstance(query, DisclosurePartition):
                rows = (contract, risk) if query.market == "sse" and query.start <= _SIGNED.date() <= query.end else ()
                return DisclosurePage(rows, len(rows), False)
            return super().fetch(query, **kwargs)

        def document(self, item, *, route="standard"):
            return fixtures._extraction(body if item == contract else "本公司收到中国证监会立案告知书，公司涉嫌信息披露违法违规。")

        def annual_income(self, request, *, observed_at):
            fact = _read(index)
            assert fact.financial.parent_net_profit == -1 and fact.risk_events and fact.positive_events
            return super().annual_income(request, observed_at=observed_at)

        def financial_reports(self, request, *, observed_at):
            self.report_calls += 1
            return ()

    source = Source(contract)
    result = fixtures._sync_company_evidence(
        CompanyEvidenceDependencies(index, source, source, lambda: 0, coalesce_publications=True),
        contract.codes, fixtures.NOW,
    )
    assert result.published_codes == 1 and source.annual_calls == source.report_calls == 1
    fact = _read(index)
    assert fact.risk_events and fact.financial.parent_net_profit == -1
    assert fact.positive_events[0].measurements.reference_annual_revenue_cny == _OPERATING_REVENUE


@pytest.mark.parametrize("coalesce", (False, True))
def test_news_checkpoint_new_body_stays_pending_and_final_seal_never_replans(tmp_path, monkeypatch, coalesce):
    index = fixtures._index(tmp_path)
    annual = fixtures._disclosure("late-report", "2025年年度报告", when=_REPORTED)
    request = CompanyFinancialReportRequest("600001", _financial().report_date, _REPORTED)

    class Source(fixtures.Sources):
        news_done = False
        report_calls = 0

        def news(self, code, **kwargs):
            self.news_done = True
            return ()

        def financial_reports(self, request, *, observed_at):
            self.report_calls += 1
            return (annual,)

    source = Source()
    plan = index.report_input_plan
    late_plans = []

    def late_requirement(codes, observed_at):
        if source.news_done:
            late_plans.append(observed_at)
            return CompanyReportInputPlan(observed_at, (), (request,))
        return plan(codes, observed_at)

    monkeypatch.setattr(index, "report_input_plan", late_requirement)
    result = fixtures._sync_company_evidence(
        CompanyEvidenceDependencies(index, source, source, lambda: 0, coalesce_publications=coalesce),
        annual.codes, fixtures.NOW, news_codes=annual.codes,
    )
    assert source.report_calls == 1 and late_plans == [fixtures.NOW]
    assert result.published_codes == 1 and result.pending_documents == 1 and result.needs_retry
    assert "risk_documents:pending" in result.failures
    with SqliteSession(index.path) as collected:
        assert tuple(collected.run("SELECT body_checked,body_retained FROM disclosures").require_one()) == (0, 0)

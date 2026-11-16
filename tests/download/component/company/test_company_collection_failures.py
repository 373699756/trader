"""A local collection failure stops requests without discarding accepted inputs."""

import sqlite3
from dataclasses import replace

import pytest

from tests.helpers.download.company import company_evidence as fixtures
from tests.helpers.download.company.company_annual_income import _contract, _financial, _income, _read, _Sources, _REPORTED
from trader.download.application.company_evidence import CompanyEvidenceDependencies, _SyncState
from trader.download.application.company_operations import CompanyOperations
from trader.download.application.company_report_inputs import CollectCompanyReportInputs, PlanCompanyReportInputs
from trader.download.application.company_structured import CollectCompanyStructured
from trader.download.domain.company_evidence import StructuredCompanyPage
from trader.download.infra.company_facts_index import ReadCompanyPublicationIdentity
from trader.infra.sqlite_connection import SqliteSession


def _identity(index):
    with SqliteSession(index.facts_path) as connection:
        return ReadCompanyPublicationIdentity(connection)


def _state(dependencies):
    return _SyncState(CompanyOperations(None, dependencies.monotonic, ()), 1)


@pytest.mark.parametrize("stage", ("financial", "annual_income", "financial_reports"))
def test_real_sqlite_commit_failure_stops_sync_and_preserves_publication_and_prior_inputs(tmp_path, stage):
    index = fixtures._index(tmp_path)
    contract = _contract(index)
    annual = fixtures._disclosure("new-annual-report", "2025年年度报告", when=_REPORTED)

    class Source(_Sources):
        requested = ()
        before_failure = None

        def _requested(self, component):
            self.requested += (component,)
            if component == stage:
                self.before_failure = _identity(index)

        def fetch(self, query, **kwargs):
            self._requested(query.component)
            if query.component == "financial":
                return StructuredCompanyPage("financial", 1, 1, 1, financials=(_financial(basic_eps=2),))
            return super().fetch(query, **kwargs)

        def annual_income(self, request, *, observed_at):
            self._requested("annual_income")
            return super().annual_income(request, observed_at=observed_at)

        def financial_reports(self, request, *, observed_at):
            self._requested("financial_reports")
            return (annual,)

        def news(self, code, **kwargs):
            self._requested("news")
            return ()

    source = Source(contract)
    triggers = {
        "financial": "BEFORE UPDATE ON financials",
        "annual_income": "BEFORE INSERT ON annual_income",
        "financial_reports": "BEFORE INSERT ON disclosures WHEN NEW.source_key='new-annual-report'",
    }
    with SqliteSession(index.path) as connection:
        connection.run(f"CREATE TRIGGER fail_collection {triggers[stage]} BEGIN SELECT RAISE(ABORT,'fixture_disk_failure'); END")
        if stage == "financial":
            connection.run("DELETE FROM component_checks WHERE component='financial'")
    with pytest.raises(RuntimeError, match="company_evidence_persistence_unavailable") as error:
        fixtures._sync_company_evidence(
            CompanyEvidenceDependencies(index, source, source, lambda: 0),
            contract.codes, fixtures.NOW, news_codes=contract.codes,
        )
    assert isinstance(error.value.__cause__, sqlite3.IntegrityError)
    assert source.requested[-1] == stage and source.requested.count(stage) == 1
    assert source.before_failure is not None and _identity(index) == source.before_failure
    assert _read(index).positive_events and _read(index).financial.basic_eps == 1
    with SqliteSession(index.path) as connection:
        assert connection.run("SELECT count(*) FROM annual_income").require_one()[0] == int(stage == "financial_reports")
        assert connection.run("SELECT count(*) FROM disclosures WHERE source_key='new-annual-report'").require_one()[0] == 0
        connection.run("DROP TRIGGER fail_collection")
    if stage == "financial_reports":
        restarted = fixtures._index(tmp_path)
        resumed_source = Source(contract)
        dependencies = CompanyEvidenceDependencies(restarted, resumed_source, resumed_source, lambda: 0)
        state = _state(dependencies)
        plan = PlanCompanyReportInputs(dependencies, contract.codes, fixtures.NOW, state)
        assert plan.annual_income == () and len(plan.financial_reports) == 1
        CollectCompanyReportInputs(dependencies, plan, state)
        assert resumed_source.annual_calls == 0 and resumed_source.requested == ("financial_reports",)


@pytest.mark.parametrize("error", (TimeoutError("fixture"), RuntimeError("fixture"), ValueError("fixture")))
def test_supplier_failure_remains_bounded_and_allows_the_other_input_source(tmp_path, error):
    index = fixtures._index(tmp_path)
    contract = _contract(index)

    class Source(_Sources):
        report_calls = 0

        def annual_income(self, request, *, observed_at):
            self.annual_calls += 1
            raise error

        def financial_reports(self, request, *, observed_at):
            self.report_calls += 1
            return ()

    source = Source(contract)
    dependencies = CompanyEvidenceDependencies(index, source, source, lambda: 0)
    state = _state(dependencies)
    plan = PlanCompanyReportInputs(dependencies, contract.codes, fixtures.NOW, state)
    assert CollectCompanyReportInputs(dependencies, plan, state) == 0
    assert source.annual_calls == source.report_calls == 1 and len(state.failures) == 1
    assert CollectCompanyReportInputs(dependencies, plan, state) == 0
    assert source.annual_calls == source.report_calls == 1


@pytest.mark.parametrize("stage", ("annual_income", "financial_reports"))
def test_invalid_supplier_response_is_deferred_without_becoming_a_local_failure(tmp_path, stage):
    index = fixtures._index(tmp_path)
    contract = _contract(index)

    class Source(_Sources):
        report_calls = 0

        def annual_income(self, request, *, observed_at):
            self.annual_calls += 1
            return (_income(code="600002" if stage == "annual_income" else request.code),)

        def financial_reports(self, request, *, observed_at):
            self.report_calls += 1
            return (fixtures._disclosure("wrong-report", "2025年年度报告", when=fixtures.NOW),) if stage == "financial_reports" else ()

    source = Source(contract)
    dependencies = CompanyEvidenceDependencies(index, source, source, lambda: 0)
    state = _state(dependencies)
    plan = PlanCompanyReportInputs(dependencies, contract.codes, fixtures.NOW, state)
    CollectCompanyReportInputs(dependencies, plan, state)
    assert source.annual_calls == source.report_calls == 1
    expected = "company_annual_income_identity_mismatch" if stage == "annual_income" else "company_annual_report_input_invalid"
    assert state.failures == (f"annual_income:{expected}",)


@pytest.mark.parametrize("stage", ("annual_income", "financial_reports", "structured"))
def test_cancelled_local_commit_retains_the_cancelled_outcome(tmp_path, monkeypatch, stage):
    index = fixtures._index(tmp_path)
    contract = _contract(index)
    cancelled = False

    def interrupt(*args):
        nonlocal cancelled
        cancelled = True
        raise InterruptedError("company_sync_cancelled")

    source = _Sources(contract)
    dependencies = CompanyEvidenceDependencies(index, source, source, lambda: 0, cancel_requested=lambda: cancelled)
    state = _state(dependencies)
    plan = PlanCompanyReportInputs(dependencies, contract.codes, fixtures.NOW, state)
    if stage == "structured":
        with SqliteSession(index.path) as connection:
            connection.run("DELETE FROM component_checks WHERE component='financial'")
        monkeypatch.setattr(index, "commit_structured", interrupt)
        CollectCompanyStructured(dependencies, "financial", fixtures.NOW, state)
    else:
        monkeypatch.setattr(index, f"commit_{stage}", interrupt)
        if stage == "financial_reports":
            monkeypatch.setattr(source, "financial_reports", lambda *args, **kwargs: ())
        CollectCompanyReportInputs(dependencies, plan, state)
    assert cancelled and state.failures == ()


def test_missing_local_population_is_not_a_supplier_response_failure(tmp_path):
    index = fixtures._index(tmp_path)
    contract = _contract(index)
    source = _Sources(contract)
    dependencies = CompanyEvidenceDependencies(index, source, source, lambda: 0)
    state = _state(dependencies)
    plan = PlanCompanyReportInputs(dependencies, contract.codes, fixtures.NOW, state)
    invalid = replace(plan.annual_income[0], code="600002")
    with pytest.raises(ValueError, match="company_annual_income_population_mismatch"):
        CollectCompanyReportInputs(dependencies, replace(plan, annual_income=(invalid,)), state)
    assert state.failures == ()


@pytest.mark.parametrize("stage", ("annual_income", "financial_reports", "financial", "pledge"))
@pytest.mark.parametrize("operation_state", ("queued", "completed"))
def test_observation_failure_is_not_recovered_as_a_supplier_failure(tmp_path, stage, operation_state):
    index = fixtures._index(tmp_path)
    contract = _contract(index)
    source = _Sources(contract)
    dependencies = CompanyEvidenceDependencies(index, source, source, lambda: 0)
    state = _state(dependencies)
    plan = PlanCompanyReportInputs(dependencies, contract.codes, fixtures.NOW, state)
    operation_kind = "annual_income" if stage in {"annual_income", "financial_reports"} else "structured"

    def observe(event):
        if event.operation is not None and event.operation.kind == operation_kind and event.operation.state == operation_state:
            raise RuntimeError("fixture_observation_failed")

    state.operations = CompanyOperations(observe, dependencies.monotonic, ())
    with pytest.raises(RuntimeError, match="fixture_observation_failed"):
        if stage in {"financial", "pledge"}:
            with SqliteSession(index.path) as connection:
                connection.run("DELETE FROM component_checks WHERE component=?", (stage,))
            CollectCompanyStructured(dependencies, stage, fixtures.NOW, state)
        else:
            if stage == "financial_reports":
                plan = replace(plan, annual_income=())
            CollectCompanyReportInputs(dependencies, plan, state)
    assert state.failures == ()

"""The sole composition root preserves local processing and precise data boundaries."""

from dataclasses import replace

import pytest

from tests.helpers.download.company.company_evidence import NOW
from tests.helpers.shared.configuration import RuntimeSettingsFixture
from tests.helpers.shared.reference_fixtures import PreparedFixture
from trader import bootstrap
from trader.download.domain.company_evidence import CompanyDownloadStore
from trader.download.domain.company_sync import CompanySyncFamily, CompanySyncRoundScope
from trader.download.infra.company_facts_reader import SQLiteCompanyEvidenceReader
from trader.download.infra.download_settings import LoadCompanySyncConfiguration
from trader.infra.http_transport import RequestsHttpTransport
from trader.infra.settings.data_paths import DataPaths
from trader.infra.sqlite_connection import SqliteSession


def _settings(tmp_path, monkeypatch):
    settings = replace(RuntimeSettingsFixture(), data_paths=DataPaths(tmp_path / "data"))
    configuration = replace(LoadCompanySyncConfiguration(settings.project_root), round_request_limit=0)
    monkeypatch.setattr(bootstrap, "LoadRuntimeSettings", lambda _: settings)
    monkeypatch.setattr(bootstrap, "_prepare_reference", lambda *args, **kwargs: PreparedFixture(NOW))
    monkeypatch.setattr(bootstrap, "_utc_now", lambda: NOW)
    monkeypatch.setattr("trader.download.infra.download_settings.LoadCompanySyncConfiguration", lambda _: configuration)
    calls = []

    def forbidden(request):
        calls.append(request.url)
        raise AssertionError("zero source allowance must prevent every physical request")

    def transport(**kwargs):
        return RequestsHttpTransport(execute_request=forbidden, **kwargs)

    monkeypatch.setattr(bootstrap, "OpenExternalHttp", transport)
    return settings, calls


def test_normal_composition_runs_local_fields_with_zero_source_allowance(tmp_path, monkeypatch):
    settings, physical = _settings(tmp_path, monkeypatch)
    result = bootstrap.ExecuteSync(settings.config_path, "company")
    assert result.exit_code == 1 and result.tasks[0].status == "pending"
    company = result.tasks[0].company
    assert company is not None and company.budget_exhausted and not company.business_complete
    assert tuple(item.family for item in company.families) == tuple(CompanySyncFamily)
    fields, = (item for item in company.families if item.family is CompanySyncFamily.FIELDS)
    assert fields.result.processed_scope_complete and not fields.result.can_continue
    assert physical == [] and sum(item.source_calls for item in company.families) == 0
    strategy = bootstrap.LoadStrategySettings(settings.strategy_config_path)
    reader = SQLiteCompanyEvidenceReader(settings.data_paths.company, event_policy=strategy.company_scoring.events,
        sqlite_policy=bootstrap.BuildSqlitePolicy(settings))
    facts = reader.read(company.scope.codes)
    assert len(facts) == len(company.scope.codes) and all(item.financial is None for item in facts)
    assert reader.publication_identity() == company.publication_hash


def test_normal_composition_uses_recent_published_news_population_without_creating_freezes(tmp_path, monkeypatch):
    settings, physical = _settings(tmp_path, monkeypatch)
    observed = []

    def selected(records, observed_at, *, check):
        check()
        assert len(records) == 4
        observed.append(observed_at)
        return ("600001", "600009")

    monkeypatch.setattr("trader.recommendation.application.pipeline.freeze_publish.recent_news_codes.RecentPublishedNewsCodes", selected)
    result = bootstrap.ExecuteSync(settings.config_path, "company")
    assert observed == [NOW] and not physical
    scope = result.tasks[0].company.scope
    with SqliteSession(settings.data_paths.company_inputs) as connection:
        selected_codes = tuple(row[0] for row in connection.run(
            "SELECT task FROM company_source_task WHERE scope=? AND family='news_scope' AND task<>'initialized' ORDER BY task",
            (scope.identity,)))
    assert selected_codes == ("600001",)
    assert all(not (settings.data_paths.recommendation(profile) / "freeze.sqlite3").exists()
        for profile in ("v1", "v2", "v3", "v5"))


def test_old_formal_schema_is_identified_before_other_company_stores_are_created(tmp_path, monkeypatch):
    settings, physical = _settings(tmp_path, monkeypatch)
    path = settings.data_paths.company
    path.parent.mkdir(parents=True)
    with SqliteSession(path) as connection:
        connection.run("CREATE TABLE obsolete(value TEXT)")
    before = path.read_bytes()
    result = bootstrap.ExecuteSync(settings.config_path, "company")
    task, = result.tasks
    assert task.status == "failed" and task.incompatible_company_store is CompanyDownloadStore.FACTS
    assert path.read_bytes() == before and physical == []
    assert not settings.data_paths.company_collection.exists()
    assert not settings.data_paths.company_normalized.exists()
    assert not settings.data_paths.company_inputs.exists()
    assert not settings.data_paths.company_sync.exists()


@pytest.mark.parametrize("cancelled", (False, True))
def test_local_probe_budget_or_user_cancel_pauses_before_creating_company_stores(tmp_path, monkeypatch, cancelled):
    settings, physical = _settings(tmp_path, monkeypatch)
    clock, cancel = [100.0], [False]
    monkeypatch.setattr(bootstrap.time, "monotonic", lambda: clock[0])

    def stop(*args, deadline, check, **kwargs):
        if cancelled:
            cancel[0] = True
        else:
            clock[0] = deadline
        check()

    monkeypatch.setattr("trader.download.infra.company_document_ocr.DetectCompanyOcr", stop)
    result = bootstrap.ExecuteSync(settings.config_path, "company", cancel_requested=lambda: cancel[0])
    company = result.tasks[0].company
    assert company is not None and company.paused and not company.processing_complete
    assert company.cancelled is cancelled and company.budget_exhausted is (not cancelled)
    assert company.families == () and company.publication_hash == "" and physical == []
    assert result.exit_code == (130 if cancelled else 1)
    assert not settings.data_paths.company.exists() and not settings.data_paths.company_sync.exists()


def test_unknown_local_probe_interruption_keeps_its_original_exception(tmp_path, monkeypatch):
    settings, physical = _settings(tmp_path, monkeypatch)
    error = InterruptedError("unclassified_local_tool_interruption")

    def unknown(*args, **kwargs):
        raise error

    monkeypatch.setattr("trader.download.infra.company_document_ocr.DetectCompanyOcr", unknown)
    with pytest.raises(InterruptedError) as caught:
        bootstrap.ExecuteCompanySync(settings, CompanySyncRoundScope(("600001",), NOW), cancel_requested=lambda: False,
            report=bootstrap.CreateSyncProgress(), fill=False)
    assert caught.value is error and not physical

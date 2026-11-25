"""The sole composition root preserves local processing and precise data boundaries."""

from dataclasses import replace
from datetime import date, timedelta

import pytest

from tests.helpers.download.company.company_evidence import NOW
from tests.helpers.shared.configuration import RuntimeSettingsFixture
from tests.helpers.shared.reference_fixtures import PreparedFixture
from trader import bootstrap
from trader.download.domain.company_evidence import CompanyDownloadStore, CompanyFinancial
from trader.download.domain.company_source_records import (
    CompanyFinancialMissingField, CompanyFinancialSourceObservation, CompanyFinancialSourcePage,
    CompanyFinancialSourceRecordHash,
)
from trader.download.domain.company_sync import COMPANY_SYNC_FAMILIES, CompanySyncFamily, CompanySyncRoundScope
from trader.download.infra.company_facts_reader import SQLiteCompanyEvidenceReader
from trader.download.infra.company_structured_source import EastmoneyCompanySource
from trader.download.infra.company_sync_runtime import CompanySyncRuntime
from trader.download.infra.download_settings import LoadCompanySyncConfiguration
from trader.download.infra.terminal_progress import RenderProgress
from trader.entrypoints.cli import Main
from trader.infra.http_contracts import HttpResponse
from trader.infra.http_transport import RequestsHttpTransport
from trader.infra.settings.data_paths import DataPaths
from trader.infra.sqlite_connection import SqliteSession
from trader.download.entrypoints.sync_progress import SyncProgress


def _settings(tmp_path, monkeypatch, *, allowance=0):
    settings = replace(RuntimeSettingsFixture(), data_paths=DataPaths(tmp_path / "data"))
    configuration = replace(LoadCompanySyncConfiguration(settings.project_root), round_request_limit=allowance)
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
    assert tuple(item.family for item in company.families) == COMPANY_SYNC_FAMILIES
    fields, = (item for item in company.families if item.family is CompanySyncFamily.FIELDS)
    assert fields.result.processed_scope_complete and not fields.result.can_continue
    assert physical == [] and sum(item.source_calls for item in company.families) == 0
    strategy = bootstrap.LoadStrategySettings(settings.strategy_config_path)
    reader = SQLiteCompanyEvidenceReader(settings.data_paths.company, event_policy=strategy.company_scoring.events,
        sqlite_policy=bootstrap.BuildSqlitePolicy(settings))
    facts = reader.read(company.scope.codes)
    assert len(facts) == len(company.scope.codes) and all(item.financial is None for item in facts)
    assert reader.publication_identity() == company.publication_hash


def test_normal_composition_projects_confirmed_step_progress_without_http(tmp_path, monkeypatch):
    settings, physical = _settings(tmp_path, monkeypatch)
    messages = []
    clock = iter(range(10000))
    progress = SyncProgress(messages.append, lambda: float(next(clock)))
    result = bootstrap.ExecuteSync(settings.config_path, "company", report=progress)
    assert result.tasks[0].company is not None and physical == []
    assert any(field.text == "最近确认调度步" for message in messages for field in message.fields)
    before = len(messages)
    progress.heartbeat()
    assert len(messages) == before


def test_company_deadline_reports_last_checked_fields_without_a_final_read(tmp_path, monkeypatch):
    settings, physical = _settings(tmp_path, monkeypatch)
    clock = [100.0]
    timeout = LoadCompanySyncConfiguration(settings.project_root).round_timeout_seconds
    monkeypatch.setattr(bootstrap.time, "monotonic", lambda: clock[0])
    calls = []
    read_counts = SQLiteCompanyEvidenceReader.read_field_counts

    def observe_read(reader, **kwargs):
        calls.append(clock[0])
        return read_counts(reader, **kwargs)

    def expire_during_fields(_runtime, _scope, *, check, **_kwargs):
        clock[0] += timeout
        check()

    monkeypatch.setattr(SQLiteCompanyEvidenceReader, "read_field_counts", observe_read)
    monkeypatch.setattr(CompanySyncRuntime, "prepare_local", expire_during_fields)
    messages = []
    progress = SyncProgress(messages.append, lambda: clock[0])
    result = bootstrap.ExecuteSync(settings.config_path, "company", report=progress)

    company = result.tasks[0].company
    assert company is not None and company.budget_exhausted and not company.publication_hash
    assert result.exit_code == 1 and calls == [100.0] and physical == []
    logs = "\n".join(RenderProgress(message) for message in messages)
    assert f"已到公司轮次时限 {timeout:g}秒" in logs
    assert "本轮收尾 | 来源 本地正式公司库 | 收尾未重新核对" in logs
    assert logs.count("company·正式库·EPS") == 2


@pytest.mark.parametrize("rejected", (False, True))
def test_company_cli_reports_three_stores_and_fixed_ledger_on_resume(tmp_path, monkeypatch, capsys, rejected):
    settings, physical = _settings(tmp_path, monkeypatch, allowance=1)
    monkeypatch.setattr("trader.entrypoints.cli.LoadRuntimeSettings", lambda _: settings)
    monkeypatch.setattr(bootstrap, "PrepareRuntimeDisk", lambda runtime, **_kwargs: runtime)

    def execute(request):
        physical.append(request.url)
        return HttpResponse(200, b"fixture")

    def financial(source, query, *, observed_at):
        source._session.get("https://example.test/financial", timeout=1)
        if rejected:
            return CompanyFinancialSourcePage(2, 2, 2, 1, (), "a" * 64)
        disclosed_at = observed_at - timedelta(days=1)
        report = CompanyFinancial("600001", date(2026, 9, 30), disclosed_at, 2.0, None, 1, 3, 4, 5, 100, 90)
        identity = "600001.SH"
        observation = CompanyFinancialSourceObservation(
            report, identity, disclosed_at, disclosed_at, observed_at, "CNY",
            CompanyFinancialSourceRecordHash(report, identity, disclosed_at, disclosed_at, "CNY"),
            (CompanyFinancialMissingField("book_value_per_share"),),
        )
        return CompanyFinancialSourcePage(query.page, 1, 1, 1, (observation,), "a" * 64)

    monkeypatch.setattr(bootstrap, "OpenExternalHttp", lambda **kwargs: RequestsHttpTransport(execute_request=execute, **kwargs))
    monkeypatch.setattr(EastmoneyCompanySource, "financial_observations", financial)
    argv = ["--config", str(settings.config_path), "sync", "company"]
    assert Main(argv) == 1
    first = capsys.readouterr()
    assert "来源库：" in first.out and "正式库：" in first.out and "请求账本：" in first.out
    assert "请求额度已用完" in first.out
    assert all(f"company·{name}" in first.err for name in ("来源库", "正式库", "请求账本"))
    assert "本次新增1次" in first.err and "轮次累计1/1次" in first.err and len(physical) == 1
    assert "正式库·EPS" in first.err and "完成0/1（0.00%）股" in first.err
    if rejected:
        error, = (line for line in first.err.splitlines() if "保留原回执" in line)
        assert "东财财务第1步" in error and "第1页" in error and "company_round_page_number_mismatch" in error
        assert "无已取得报告" in first.err
    else:
        assert "已入库1/1（100.00%）页" in first.err
        assert "来源库·EPS" in first.err and "有值1/1（100.00%）股" in first.err
        assert "来源库·每股净资产" in first.err and "缺值1/1（100.00%）股" in first.err

    assert Main([*argv, "fill"]) == 1
    resumed = capsys.readouterr()
    assert "本次新增0次" in resumed.err and "轮次累计1/1次" in resumed.err and len(physical) == 1
    with SqliteSession(settings.data_paths.company_sources) as sources, SqliteSession(settings.data_paths.company_sync) as ledger:
        assert sources.run("SELECT COUNT(*) FROM company_round_page").require_one()[0] == (0 if rejected else 1)
        assert ledger.run("SELECT COUNT(*) FROM company_sync_request").require_one()[0] == 1


def test_normal_composition_creates_only_three_company_stores(tmp_path, monkeypatch):
    settings, physical = _settings(tmp_path, monkeypatch)
    result = bootstrap.ExecuteSync(settings.config_path, "company")
    assert result.tasks[0].company is not None and not physical
    paths = settings.data_paths
    assert set(paths.company.parent.rglob("*.sqlite3")) == {paths.company, paths.company_sources, paths.company_sync}
    with SqliteSession(paths.company_sources) as connection:
        tables = {row[0] for row in connection.run("SELECT name FROM sqlite_schema WHERE type='table'")}
        assert {"disclosures", "company_financial_observation", "company_source_task", "company_round_intent"} <= tables
        assert not {"annual_income", "company_expired_ranges", "company_sync_job", "component_checks", "structured_members", "structured_progress"} & tables
        assert not connection.run("SELECT 1 FROM company_source_task WHERE family='news_scope'").fetchall()
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
    assert not settings.data_paths.company_sources.exists()
    assert not settings.data_paths.company_sync.exists()


def test_company_sync_uses_current_database_names_when_other_files_exist(tmp_path, monkeypatch):
    settings, physical = _settings(tmp_path, monkeypatch)
    paths = settings.data_paths
    unrelated = tuple(paths.company.parent / name for name in (
        "company_facts.sqlite3", "company_normalized.sqlite3", "company_inputs.sqlite3", "raw/company_collection.sqlite3",
    ))
    for path in unrelated:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"must remain intact")
    result = bootstrap.ExecuteSync(settings.config_path, "company")
    task, = result.tasks
    assert task.company is not None and task.incompatible_company_store is None
    assert not physical and all(path.read_bytes() == b"must remain intact" for path in unrelated)
    assert all(path.is_file() for path in (paths.company, paths.company_sources, paths.company_sync))


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
            report=bootstrap.CreateSyncProgress())
    assert caught.value is error and not physical

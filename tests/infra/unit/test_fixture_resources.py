"""Fixture injection and cleanup are observable without production I/O."""

from dataclasses import replace
import threading

import pytest

from tests.helpers.shared.worker_cleanup import StopFixtureWorkers
from trader.infra.shutdown import ShutdownDeadline
from trader.infra.workers import BoundedExecutor


def test_fixture_cleanup_reports_blocked_owned_work_with_a_finite_deadline() -> None:
    pool = BoundedExecutor(worker_count=1, queue_capacity=0, thread_name_prefix="fixture-cleanup-regression")
    entered, release = threading.Event(), threading.Event()

    def wait_for_release() -> None:
        entered.set()
        assert release.wait(3), "controlled fixture worker was not released"

    pool.start()
    try:
        assert pool.submit(wait_for_release) is not None
        assert entered.wait(1)
        with pytest.raises(AssertionError, match="fixture worker cleanup incomplete"):
            StopFixtureWorkers(pool, timeout_seconds=0.02)
    finally:
        release.set()
        assert pool.stop(cancel_futures=True, deadline=ShutdownDeadline.start(2)).completed


def test_explicit_business_configuration_does_not_reload_default_settings(monkeypatch) -> None:
    from tests.helpers.download import configuration as download
    from tests.helpers.recommendation import configuration as recommendation
    from tests.helpers.training import configuration as training

    policy = recommendation.CompanyScoringPolicyFixture()
    filters = recommendation.HardFilterPolicyFixture()
    events = download.CompanyEventPolicyFixture()
    failures = download.DownloadFailureConfigurationFixture()
    fitting = training.TcnFittingParametersFixture()
    ranker = training.V1TrainingSettingsFixture()

    def unexpected_reload():
        pytest.fail("explicit fixture configuration must not read default settings")

    monkeypatch.setattr(recommendation, "StrategySettingsFixture", unexpected_reload)
    monkeypatch.setattr(download, "StrategySettingsFixture", unexpected_reload)
    monkeypatch.setattr(download, "_failure_settings", unexpected_reload)
    monkeypatch.setattr(training, "_tcn_settings", unexpected_reload)
    monkeypatch.setattr(training, "_v1_settings", unexpected_reload)

    assert recommendation.CompanyScoringPolicyFixture(policy=policy, d25_cap=0) == replace(policy, d25_cap=0)
    assert recommendation.HardFilterPolicyFixture(policy=filters) == filters
    assert download.CompanyEventPolicyFixture(policy=events) == events
    assert download.DownloadFailureConfigurationFixture(configuration=failures) == failures
    assert training.TcnFittingParametersFixture(settings=fitting) == fitting
    assert training.V1TrainingSettingsFixture(settings=ranker) == ranker


def test_explicit_resource_settings_do_not_eagerly_load_fixture_defaults(tmp_path, monkeypatch) -> None:
    from tests.helpers.download import company_resources as download
    from tests.helpers.download.configuration import CompanyEventPolicyFixture
    from tests.helpers.recommendation import company_research as recommendation
    from tests.helpers.recommendation.configuration import CompanyScoringPolicyFixture
    from tests.helpers.shared.configuration import RuntimeSettingsFixture, SqlitePolicyFixture

    event_policy = CompanyEventPolicyFixture()
    company_policy = CompanyScoringPolicyFixture()
    preload = RuntimeSettingsFixture().pipeline.company_preload
    sqlite_policy = SqlitePolicyFixture()

    def unexpected_reload():
        pytest.fail("explicit resource inputs must not load fixture defaults")

    monkeypatch.setattr(download, "CompanyEventPolicyFixture", unexpected_reload)
    monkeypatch.setattr(download, "CompanySyncConfigurationFixture", unexpected_reload)
    monkeypatch.setattr(download, "SqlitePolicyFixture", unexpected_reload)
    monkeypatch.setattr(recommendation, "CompanyScoringPolicyFixture", unexpected_reload)
    monkeypatch.setattr(recommendation, "RuntimeSettingsFixture", unexpected_reload)

    reader = download.SQLiteCompanyEvidenceReaderFixture(
        tmp_path / "absent.sqlite3", event_policy=event_policy, sqlite_policy=sqlite_policy,
    )
    publication = recommendation.PublishedCompanyResearchFixture(
        reader, wall_clock=lambda: pytest.fail("clock must not run during construction"),
        open_dates=lambda: (), monotonic=lambda: 0,
        company_policy=company_policy, preload_settings=preload,
    )
    text = download.CompanyDocumentFilesFixture(
        tmp_path / "text", max_text_characters=100, max_bytes=4096, reserve_bytes=1,
    )
    assert publication is not None and text.root == tmp_path / "text"
    assert not (tmp_path / "absent.sqlite3").exists()

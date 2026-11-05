"""The composition root enforces each CNInfo host's rate across its real clients."""

from contextlib import contextmanager
from dataclasses import replace

import pytest

from tests.component import test_company_evidence as fixtures
from tests.helpers.configuration import DownloadFailureConfigurationFixture, SqlitePolicyFixture
from trader import bootstrap
from trader.download.domain.company_evidence import CompanyEvidenceSyncResult
from trader.download.infra.company_http import CompanyRequestPacer
from trader.infra.settings.data_paths import DataPaths


@pytest.mark.parametrize("fail", (False, True))
def test_directory_does_not_consume_the_download_host_budget_and_all_clients_close(tmp_path, monkeypatch, fail):
    opened, closed = [], []
    clock = [0.0]

    @contextmanager
    def http():
        client = object()
        opened.append(client)
        try:
            yield client
        finally:
            closed.append(client)

    def sleep(delay):
        clock[0] += delay

    def pacer(interval, monotonic, sleeper, cancelled):
        return CompanyRequestPacer(interval, lambda: clock[0], sleep, cancelled)

    expected = CompanyEvidenceSyncResult(fixtures.NOW, 1, 1, 0, 0, 0, 0, (), 0)

    def sync(dependencies, request):
        assert len(dependencies.document_sources) == request.configuration.document_workers == 2
        first, second = dependencies.document_sources
        # Separate hosts can start together; both download clients still share
        # the original one-second request budget for static.cninfo.com.cn.
        first._pacer.wait()
        dependencies.disclosures._pacer.wait()
        assert clock[0] == 0
        second._pacer.wait()
        assert clock[0] == pytest.approx(request.configuration.request_interval_seconds)
        first._pacer.wait()
        assert clock[0] == pytest.approx(2 * request.configuration.request_interval_seconds)
        if fail:
            raise RuntimeError("fixture_sync_failure")
        return expected

    monkeypatch.setattr(bootstrap, "OpenExternalHttp", http)
    monkeypatch.setattr(bootstrap, "CompanyRequestPacer", pacer)
    monkeypatch.setattr(bootstrap, "SyncCompanyEvidence", sync)
    monkeypatch.setattr(bootstrap.SQLiteCompanyEvidenceIndex, "publish_file", lambda self: None)
    request = fixtures.CompanyEvidenceRequest(
        ("600001",), fixtures.NOW, replace(fixtures._TEST_COMPANY_CONFIGURATION, document_workers=2),
    )

    def run():
        return bootstrap._sync_company_evidence(
            DataPaths(tmp_path), None, request, sqlite_policy=SqlitePolicyFixture(),
            failure_configuration=DownloadFailureConfigurationFixture(),
        )

    if fail:
        with pytest.raises(RuntimeError, match="fixture_sync_failure"):
            run()
    else:
        assert run() is expected
    assert len(opened) == 4 and len(closed) == 4 and set(opened) == set(closed)

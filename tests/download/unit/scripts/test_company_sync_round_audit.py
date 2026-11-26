"""An isolated company audit uses its explicit budget throughout restart."""

from datetime import datetime

from scripts.download.company.company_sync_round_audit import (
    AuditCompanySyncRestart, AuditCompanySyncRounds, PrepareCompanySyncCliAudit,
)
from tests.helpers.shared.configuration import RuntimeSettingsFixture
from trader import bootstrap
from trader.download.domain.company_sync import CompanySyncRoundScope
from trader.download.infra.download_settings import LoadCompanySyncConfiguration
from trader.infra.http_transport import RequestsHttpTransport


def test_cli_audit_keeps_explicit_duration_and_request_limits(tmp_path):
    settings = RuntimeSettingsFixture()
    directory = tmp_path / "cli"
    config = PrepareCompanySyncCliAudit(settings, directory, request_limit=2, round_seconds=15)
    assert config == directory / "config" / settings.config_path.name
    policy = LoadCompanySyncConfiguration(directory)
    assert policy.round_timeout_seconds == 15 and policy.round_request_limit == 2
    assert policy.round_step_limit is None


def test_round_audit_reopens_with_same_request_limit(tmp_path, monkeypatch):
    def forbidden(_request):
        raise AssertionError("the isolated zero-request budget must also apply on restart")

    settings = RuntimeSettingsFixture()
    monkeypatch.setattr(bootstrap, "OpenExternalHttp", lambda **kwargs: RequestsHttpTransport(execute_request=forbidden, **kwargs))
    directory = tmp_path / "rounds"
    codes = ("600001",)
    audit = AuditCompanySyncRounds(settings, directory, codes, seconds_per_round=30, request_limit=0)
    assert audit["same_finite_round"]
    scope = CompanySyncRoundScope(codes, datetime.fromisoformat(audit["cutoff"]))
    restart = AuditCompanySyncRestart(settings, directory, scope, seconds=20)
    assert restart["same_finite_round"] and restart["no_new_source_calls"]
    assert restart["after_reserved_slots"] == 0

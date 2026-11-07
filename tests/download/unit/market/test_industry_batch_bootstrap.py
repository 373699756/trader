"""Reference composition selects the user-authorized HDD original-response batch."""

from datetime import date, datetime
from dataclasses import replace
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tests.helpers.shared.configuration import SqlitePolicyFixture
from tests.helpers.shared.reference_fixtures import PreparedFixture
from trader import bootstrap
from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.infra.current_industry_publication import CurrentIndustryPublication
from trader.download.application.sync_data import SyncTarget
from trader.infra.disk_types import DiskKind, DiskMedium, DiskProfile
from trader.infra.http_contracts import HttpRequest, HttpResponse
from trader.infra.http_transport import RequestsHttpTransport
from trader.infra.market_data.observations import SourceObservation
from trader.infra.settings import LoadRuntimeSettings
from trader.infra.settings.data_paths import DataPaths


@pytest.mark.parametrize(
    "detected,configured,expected",
    (("hdd", None, 64), ("ssd", None, 1), ("unknown", None, 1),
     ("unknown", "hdd", 64), ("hdd", "ssd", 1)),
)
def test_reference_uses_hdd_batch_and_preserves_population_and_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    detected: DiskKind, configured: DiskMedium | None, expected: int,
) -> None:
    now = datetime(2026, 11, 7, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
    reference = PreparedFixture(now, open_dates=(date(2026, 11, 6), now.date()))
    batches: list[int] = []

    def reject_request(_request: HttpRequest) -> HttpResponse:
        raise AssertionError("reference composition must not request a supplier in this fixture")

    def industry(
        self: CurrentIndustryPublication, at: datetime,
        universe: tuple[BaoStockSecurity, ...], calendar: tuple[date, ...],
    ) -> tuple[SourceObservation, ...]:
        assert at == now and universe == reference.eligible_universe and calendar == reference.open_dates
        batches.append(self.source_batch_rows)
        return ()

    def automatic_profile(_path: Path, _device: int) -> DiskProfile:
        return DiskProfile(detected, "fixture", "automatic", "unit_test_fixture")

    monkeypatch.setattr(bootstrap, "LoadCurrentAShareUniverse", lambda *_args, **_kwargs: reference.official_universe)
    monkeypatch.setattr("trader.infra.disk_probe._probe_device", automatic_profile)
    monkeypatch.setattr(bootstrap, "_utc_now", lambda: now)
    monkeypatch.setattr(bootstrap.HistoryStNameSource, "fetch", lambda *_args: reference.st_evidence)
    monkeypatch.setattr(bootstrap, "FetchAkshareCalendar", lambda *_args, **_kwargs: reference.open_dates)
    monkeypatch.setattr(bootstrap, "OpenExternalHttp", lambda **kwargs: RequestsHttpTransport(**kwargs, execute_request=reject_request))
    monkeypatch.setattr(CurrentIndustryPublication, "fetch", industry)

    result = bootstrap._prepare_reference(
        tmp_path, now, None, cancel_requested=lambda: False, shutdown_timeout_seconds=5,
        sqlite_policy=SqlitePolicyFixture(), disk_medium=configured,
    )

    assert batches == [expected]
    assert result.eligible_universe == reference.eligible_universe
    assert bootstrap.SQLiteReferenceIndex(tmp_path / "company_st.sqlite3", sqlite_policy=SqlitePolicyFixture()).read() == result


@pytest.mark.parametrize("target", ("all", "history", "market", "company"))
def test_normal_sync_passes_the_configured_medium_to_shared_reference_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: SyncTarget,
) -> None:
    class ReferenceReached(Exception):
        pass

    root = Path(__file__).resolve().parents[4]
    runtime = replace(LoadRuntimeSettings(root / "config/runtime.json"), disk_medium="hdd", data_paths=DataPaths(tmp_path))

    def prepare(*_args: object, disk_medium: DiskMedium | None = None, **_kwargs: object) -> None:
        assert disk_medium == "hdd"
        raise ReferenceReached

    monkeypatch.setattr(bootstrap, "LoadRuntimeSettings", lambda _path: runtime)
    monkeypatch.setattr(bootstrap, "_prepare_reference", prepare)
    with pytest.raises(ReferenceReached):
        bootstrap.ExecuteSync(runtime.config_path, target)

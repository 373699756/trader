from __future__ import annotations

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

from trader.entrypoints import cli
from trader.recommendation.application.risk_download import (
    DownloadRiskSnapshot,
    RiskDownloadContext,
    RiskDownloadInventory,
    RiskDownloadResult,
)
from trader.recommendation.domain.market.research import ResearchObservation
from trader.recommendation.infra.market_data.research_load_status import ResearchLoadReport

SHANGHAI = ZoneInfo("Asia/Shanghai")


class _Loader:
    def __init__(self) -> None:
        self.recovered_at: datetime | None = None
        self.retained_stale = False
        self.deadline: datetime | None = None
        self.loaded_codes: tuple[str, ...] = ()
        self.force = False
        self.load_calls: list[tuple[tuple[str, ...], bool]] = []
        self.cached_observations: dict[str, ResearchObservation] = {}

    def recover_from_data_plane(self, observed_at: datetime | None = None, *, retain_stale: bool = False) -> None:
        self.recovered_at = observed_at
        self.retained_stale = retain_stale

    def cached(self, codes, **_options):
        return {code: self.cached_observations[code] for code in codes if code in self.cached_observations}

    def load_report(self, codes, observed_at, **options):
        self.deadline = options["deadline"]
        self.loaded_codes = tuple(codes)
        self.force = options["force"]
        self.load_calls.append((self.loaded_codes, self.force))
        return ResearchLoadReport(
            {codes[0]: ResearchObservation(data_version="risk:1")} if codes else {},
            changed_codes=(codes[0],) if codes else (),
            deferred_codes=(codes[1],) if len(codes) > 1 else (),
            deadline_reached=len(codes) > 1,
        )


class _AnnouncementReport:
    changed_codes = ("600001",)
    completed_codes = ("600001",)
    deferred_codes: tuple[str, ...] = ()
    failed_codes: tuple[str, ...] = ()
    pages_fetched = 3
    saved_announcements = 42
    deadline_reached = False


class _AnnouncementSync:
    def __init__(self) -> None:
        self.completed = False

    def pending_codes(self, codes, observed_at):
        del observed_at
        return () if self.completed else tuple(codes)

    def sync_report(self, codes, observed_at, *, deadline):
        del codes, observed_at, deadline
        self.completed = True
        return _AnnouncementReport()


class _EmptyAnnouncementReport:
    changed_codes: tuple[str, ...] = ()
    completed_codes: tuple[str, ...] = ()
    deferred_codes = ("600001",)
    failed_codes: tuple[str, ...] = ()
    pages_fetched = 0
    saved_announcements = 0
    deadline_reached = False


class _StuckAnnouncementSync:
    def __init__(self) -> None:
        self.report_count = 0

    def pending_codes(self, codes, observed_at):
        del observed_at
        return tuple(codes) if self.report_count < 2 else ()

    def sync_report(self, codes, observed_at, *, deadline):
        del codes, observed_at, deadline
        self.report_count += 1
        return _EmptyAnnouncementReport()


class _ChangingVersionLoader(_Loader):
    def __init__(self) -> None:
        super().__init__()
        self.recovery_count = 0

    def recover_from_data_plane(self, observed_at=None, *, retain_stale=False):
        super().recover_from_data_plane(observed_at, retain_stale=retain_stale)
        self.recovery_count += 1
        self.cached_observations["600001"] = ResearchObservation(data_version=f"risk:{self.recovery_count}")


def test_risk_download_reads_partition_before_bounded_supplier_refresh() -> None:
    loader = _Loader()
    observed_at = datetime(2026, 10, 16, 9, 15, tzinfo=SHANGHAI)

    result = DownloadRiskSnapshot(
        loader,
        ("600001", "000001"),
        RiskDownloadContext(date(2026, 10, 16), observed_at, lambda: 10.0, lambda: observed_at),
    )

    assert loader.recovered_at == observed_at
    assert loader.retained_stale is True
    assert loader.deadline is not None
    assert (loader.deadline - observed_at).total_seconds() == 120.0
    assert result.requested_codes == 2
    assert result.available_codes == 0
    assert result.covered_codes == 0
    assert result.partial_codes == 0
    assert result.changed_codes == 2
    assert result.deferred_codes == 0
    assert result.failed_codes == 2
    assert result.deadline_reached is False
    assert result.needs_retry
    assert result.wave_count == 2
    assert result.stop_reason == "source_exhausted"


def test_risk_download_resumes_unseen_codes_before_retrying_existing_partial_observations() -> None:
    loader = _Loader()
    loader.cached_observations = {
        "000001": ResearchObservation(
            announcements_available=True,
            pledge_ratio_pct=0.0,
            unlock_ratio_pct=0.0,
            source_errors=("announcements:timeout",),
        )
    }
    observed_at = datetime(2026, 10, 16, 9, 15, tzinfo=SHANGHAI)
    result = DownloadRiskSnapshot(
        loader,
        ("600001", "000001", "300001"),
        RiskDownloadContext(date(2026, 10, 16), observed_at, lambda: 10.0, lambda: observed_at),
    )

    assert loader.load_calls[:2] == [(("300001", "600001"), False), (("000001",), True)]
    assert result.stop_reason == "source_exhausted"


def test_risk_download_retries_only_recoverable_component_errors_after_initial_coverage() -> None:
    loader = _Loader()
    loader.cached_observations = {
        "000001": ResearchObservation(source_errors=("pledge:schema_invalid",)),
        "300001": ResearchObservation(source_errors=("announcements:timeout",)),
        "600001": ResearchObservation(source_errors=("financial:resource_rejected",)),
        "600002": ResearchObservation(source_errors=("unlock:cancelled",)),
    }
    observed_at = datetime(2026, 10, 16, 9, 15, tzinfo=SHANGHAI)
    DownloadRiskSnapshot(
        loader,
        ("000001", "300001", "600001", "600002"),
        RiskDownloadContext(date(2026, 10, 16), observed_at, lambda: 10.0, lambda: observed_at),
    )

    assert loader.load_calls[0] == (("300001", "600001"), True)
    assert all("000001" not in codes and "600002" not in codes for codes, _force in loader.load_calls)


def test_risk_download_runs_official_source_until_issuer_checkpoint_completes() -> None:
    loader = _Loader()
    loader.cached_observations = {"600001": ResearchObservation(data_version="existing")}
    announcement_sync = _AnnouncementSync()
    observed_at = datetime(2026, 10, 16, 9, 15, tzinfo=SHANGHAI)

    result = DownloadRiskSnapshot(
        loader,
        ("600001",),
        RiskDownloadContext(date(2026, 10, 16), observed_at, lambda: 10.0, lambda: observed_at),
        announcement_sync=announcement_sync,
    )

    assert loader.load_calls == []
    assert result.wave_count == 1
    assert result.stop_reason == "source_exhausted"
    assert result.changed_codes == 1
    assert result.official_pages_fetched == 3
    assert result.official_announcements_saved == 42
    assert result.official_failed_codes == 0


def test_risk_download_stops_when_only_volatile_cache_version_changes() -> None:
    loader = _ChangingVersionLoader()
    announcement_sync = _StuckAnnouncementSync()
    observed_at = datetime(2026, 10, 16, 9, 15, tzinfo=SHANGHAI)

    result = DownloadRiskSnapshot(
        loader,
        ("600001",),
        RiskDownloadContext(date(2026, 10, 16), observed_at, lambda: 10.0, lambda: observed_at),
        announcement_sync=announcement_sync,
    )

    assert announcement_sync.report_count == 1
    assert result.wave_count == 1
    assert result.stop_reason == "no_progress"
    assert result.deferred_codes == 1


def test_risk_download_cli_reports_component_and_retained_inventory(monkeypatch, capsys, tmp_path) -> None:
    result = RiskDownloadResult(
        target_date=date(2026, 10, 19),
        requested_codes=100,
        available_codes=80,
        covered_codes=10,
        partial_codes=70,
        changed_codes=20,
        deferred_codes=20,
        failed_codes=0,
        deadline_reached=True,
        elapsed_seconds=123.4567,
        inventory=RiskDownloadInventory(75, 10, 70, 78, 0, 3, 60, 12, 34, 567),
    )
    monkeypatch.setattr("trader.bootstrap.ExecuteRiskDownload", lambda _path: result)

    assert cli.Main(["--config", str(tmp_path / "runtime.json"), "risk_download"]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["component_codes"] == {
        "financial": 75,
        "announcements": 10,
        "pledge": 70,
        "unlock": 78,
        "regulatory_history": 0,
        "regulatory_evidence": 3,
        "news_queried": 60,
    }
    assert payload["retained_items"] == {
        "news": 12,
        "announcements": 34,
        "financial_reports": 567,
    }
    assert payload["official_source"] == {
        "pages_fetched": 0,
        "announcements_saved": 0,
        "failed_codes": 0,
    }
    assert payload["elapsed_seconds"] == 123.457

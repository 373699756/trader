from __future__ import annotations

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

from trader.entrypoints import cli
from trader.recommendation.application.risk_download import (
    DownloadRiskSnapshot,
    RiskDownloadContext,
    RiskDownloadInventory,
    RiskDownloadProgress,
    RiskDownloadResult,
)
from trader.recommendation.domain.market.research import ResearchObservation
from trader.recommendation.infra.market_data.research_load_status import ResearchLoadReport

SHANGHAI = ZoneInfo("Asia/Shanghai")


class _Loader:
    def __init__(self) -> None:
        self.recovered_at: datetime | None = None
        self.retained_stale = False
        self.recovered_codes: list[tuple[str, ...] | None] = []
        self.deadline: datetime | None = None
        self.loaded_codes: tuple[str, ...] = ()
        self.force = False
        self.load_calls: list[tuple[tuple[str, ...], bool]] = []
        self.cached_observations: dict[str, ResearchObservation] = {}

    def recover_from_data_plane(
        self,
        observed_at: datetime | None = None,
        *,
        retain_stale: bool = False,
        codes: tuple[str, ...] | None = None,
    ) -> None:
        self.recovered_at = observed_at
        self.retained_stale = retain_stale
        self.recovered_codes.append(codes)

    def cached(self, codes, **_options):
        return {code: self.cached_observations[code] for code in codes if code in self.cached_observations}

    def load_report(self, codes, observed_at, **options):
        self.deadline = options["deadline"]
        self.loaded_codes = tuple(codes)
        self.force = options["force"]
        self.load_calls.append((self.loaded_codes, self.force))
        if progress := options.get("progress"):
            progress(len(codes), len(codes))
        return ResearchLoadReport(
            {codes[0]: ResearchObservation(data_version="risk:1", pledge_ratio_pct=0.0)} if codes else {},
            changed_codes=(codes[0],) if codes else (),
            deferred_codes=(codes[1],) if len(codes) > 1 else (),
            deadline_reached=len(codes) > 1,
        )


class _AnnouncementReport:
    changed_codes = ("600001",)
    completed_codes = ("600001",)
    attempted_codes = ("600001",)
    deferred_codes: tuple[str, ...] = ()
    failed_codes: tuple[str, ...] = ()
    pages_fetched = 3
    pages_checkpointed = 3
    saved_announcements = 42
    resumed_codes = ("600001",)
    deadline_reached = False
    request_seconds = 0.0
    parse_seconds = 0.0
    evidence_write_seconds = 0.0
    cursor_write_seconds = 0.0
    persistence_error = None


class _AnnouncementSync:
    def __init__(self) -> None:
        self.completed = False

    def pending_codes(self, codes, observed_at):
        del observed_at
        return () if self.completed else tuple(codes)

    def sync_report(self, codes, observed_at, *, deadline, progress=None):
        del codes, observed_at, deadline
        self.completed = True
        if progress is not None:
            progress((1, 1, 3, 3, 42, 1))
        return _AnnouncementReport()


class _EmptyAnnouncementReport:
    changed_codes: tuple[str, ...] = ()
    completed_codes: tuple[str, ...] = ()
    attempted_codes: tuple[str, ...] = ()
    deferred_codes = ("600001",)
    failed_codes: tuple[str, ...] = ()
    pages_fetched = 0
    pages_checkpointed = 0
    saved_announcements = 0
    resumed_codes: tuple[str, ...] = ()
    deadline_reached = False
    request_seconds = 0.0
    parse_seconds = 0.0
    evidence_write_seconds = 0.0
    cursor_write_seconds = 0.0
    persistence_error = None


class _BusyThenReadyAnnouncementSync(_AnnouncementSync):
    def __init__(self) -> None:
        super().__init__()
        self.report_count = 0

    def sync_report(self, codes, observed_at, *, deadline, progress=None):
        self.report_count += 1
        if self.report_count == 1:
            report = _EmptyAnnouncementReport()
            report.persistence_error = "busy"
            return report
        return super().sync_report(codes, observed_at, deadline=deadline, progress=progress)


class _UnavailableAnnouncementSync(_AnnouncementSync):
    def sync_report(self, codes, observed_at, *, deadline, progress=None):
        del codes, observed_at, deadline, progress
        report = _EmptyAnnouncementReport()
        report.persistence_error = "unavailable"
        return report


class _StuckAnnouncementSync:
    def __init__(self) -> None:
        self.report_count = 0

    def pending_codes(self, codes, observed_at):
        del observed_at
        return tuple(codes) if self.report_count < 2 else ()

    def sync_report(self, codes, observed_at, *, deadline, progress=None):
        del codes, observed_at, deadline
        self.report_count += 1
        if progress is not None:
            progress((0, 1, 0, 0, 0, 0))
        return _EmptyAnnouncementReport()


class _CheckpointAnnouncementSync:
    def __init__(self) -> None:
        self.report_count = 0

    def pending_codes(self, codes, observed_at):
        del observed_at
        return tuple(codes) if self.report_count < 2 else ()

    def sync_report(self, codes, observed_at, *, deadline, progress=None):
        del codes, observed_at, deadline
        self.report_count += 1
        completed = ("600001",) if self.report_count == 2 else ()
        deferred = () if completed else ("600001",)
        if progress is not None:
            progress((1, 1, 1, 1, 30, len(completed)))
        return _CheckpointAnnouncementReport(completed, deferred)


class _CheckpointAnnouncementReport:
    def __init__(self, completed_codes: tuple[str, ...], deferred_codes: tuple[str, ...]) -> None:
        self.changed_codes = ("600001",)
        self.completed_codes = completed_codes
        self.attempted_codes = ("600001",)
        self.deferred_codes = deferred_codes
        self.failed_codes: tuple[str, ...] = ()
        self.pages_fetched = 1
        self.pages_checkpointed = 1
        self.saved_announcements = 30
        self.resumed_codes = completed_codes
        self.deadline_reached = bool(deferred_codes)
        self.request_seconds = 0.0
        self.parse_seconds = 0.0
        self.evidence_write_seconds = 0.0
        self.cursor_write_seconds = 0.0
        self.persistence_error = None


class _FairAnnouncementSync:
    def __init__(self) -> None:
        self.completed: set[str] = set()
        self.sync_calls: list[tuple[str, ...]] = []

    def pending_codes(self, codes, observed_at):
        del observed_at
        return tuple(code for code in codes if code not in self.completed)

    def sync_report(self, codes, observed_at, *, deadline, progress=None):
        del observed_at, deadline
        requested = tuple(codes)
        self.sync_calls.append(requested)
        attempt = len(self.sync_calls)
        if attempt == 1:
            report = _FairAnnouncementReport(
                completed_codes=("300001",),
                attempted_codes=("000001", "300001"),
                deferred_codes=("000001", "600001"),
            )
        elif attempt == 2:
            report = _FairAnnouncementReport(
                completed_codes=("600001",),
                attempted_codes=("600001",),
                deferred_codes=("000001",),
            )
        else:
            report = _FairAnnouncementReport(
                completed_codes=(),
                attempted_codes=("000001",),
                deferred_codes=("000001",),
            )
        self.completed.update(report.completed_codes)
        if progress is not None:
            progress((len(report.attempted_codes), len(requested), 0, 0, 0, len(report.completed_codes)))
        return report


class _FairAnnouncementReport:
    def __init__(
        self,
        *,
        completed_codes: tuple[str, ...],
        attempted_codes: tuple[str, ...],
        deferred_codes: tuple[str, ...],
    ) -> None:
        self.changed_codes: tuple[str, ...] = ()
        self.completed_codes = completed_codes
        self.attempted_codes = attempted_codes
        self.deferred_codes = deferred_codes
        self.failed_codes: tuple[str, ...] = ()
        self.pages_fetched = 0
        self.pages_checkpointed = 0
        self.saved_announcements = 0
        self.resumed_codes: tuple[str, ...] = ()
        self.deadline_reached = bool(deferred_codes)
        self.request_seconds = 0.0
        self.parse_seconds = 0.0
        self.evidence_write_seconds = 0.0
        self.cursor_write_seconds = 0.0
        self.persistence_error = None


class _Progress:
    def __init__(self) -> None:
        self.events: list[RiskDownloadProgress] = []

    def publish(self, progress: RiskDownloadProgress) -> None:
        self.events.append(progress)


class _ChangingVersionLoader(_Loader):
    def __init__(self) -> None:
        super().__init__()
        self.recovery_count = 0

    def recover_from_data_plane(self, observed_at=None, *, retain_stale=False, codes=None):
        super().recover_from_data_plane(observed_at, retain_stale=retain_stale, codes=codes)
        self.recovery_count += 1
        self.cached_observations["600001"] = ResearchObservation(data_version=f"risk:{self.recovery_count}")


class _OscillatingErrorLoader(_Loader):
    def __init__(self) -> None:
        super().__init__()
        self.cached_observations["600001"] = ResearchObservation(source_errors=("news:timeout",))
        self.attempts = 0

    def load_report(self, codes, observed_at, **options):
        del observed_at
        self.attempts += 1
        requested = tuple(codes)
        self.load_calls.append((requested, options["force"]))
        if progress := options.get("progress"):
            progress(len(requested), len(requested))
        error = "news:rate_limited" if self.attempts % 2 else "news:timeout"
        return ResearchLoadReport(
            {requested[0]: ResearchObservation(source_errors=(error,))},
            changed_codes=(requested[0],),
        )


class _FairRetryLoader(_Loader):
    def __init__(self) -> None:
        super().__init__()
        self.cached_observations = {
            code: ResearchObservation(source_errors=("news:timeout",)) for code in ("000001", "300001", "600001")
        }

    def load_report(self, codes, observed_at, **options):
        del observed_at
        requested = tuple(codes)
        self.load_calls.append((requested, options["force"]))
        attempt = len(self.load_calls)
        if attempt == 1:
            observations = {
                "000001": ResearchObservation(source_errors=("news:rate_limited",)),
                "300001": ResearchObservation(pledge_ratio_pct=0.0),
            }
            deferred = ("600001",)
        elif attempt == 2:
            observations = {"600001": ResearchObservation(pledge_ratio_pct=0.0)}
            deferred = ("000001",)
        else:
            observations = {"000001": ResearchObservation(source_errors=("news:timeout",))}
            deferred = ()
        if progress := options.get("progress"):
            progress(len(requested) - len(deferred), len(requested))
        return ResearchLoadReport(
            observations,
            changed_codes=tuple(observations),
            deferred_codes=deferred,
            deadline_reached=bool(deferred),
        )


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
    assert result.available_codes == 2
    assert result.covered_codes == 0
    assert result.partial_codes == 2
    assert result.changed_codes == 2
    assert result.deferred_codes == 0
    assert result.failed_codes == 0
    assert result.deadline_reached is False
    assert result.needs_retry
    assert result.wave_count == 2
    assert result.stop_reason == "source_exhausted"


def test_risk_download_reports_persisted_batches_wave_summary_and_final_state() -> None:
    loader = _Loader()
    progress = _Progress()
    observed_at = datetime(2026, 10, 16, 9, 15, tzinfo=SHANGHAI)

    result = DownloadRiskSnapshot(
        loader,
        ("600001", "000001"),
        RiskDownloadContext(
            date(2026, 10, 16),
            observed_at,
            lambda: 10.0,
            lambda: observed_at,
            progress=progress,
        ),
    )

    assert progress.events[0].stage == "local_snapshot"
    assert progress.events[0].state == "started"
    assert progress.events[0].requested_codes == 2
    assert progress.events[1].stage == "local_snapshot"
    assert progress.events[1].state == "completed"
    assert progress.events[1].deferred_codes == 2
    assert any(
        event.stage == "research_initial" and event.state == "running" and event.completed_units == event.total_units
        for event in progress.events
    )
    assert any(event.stage == "wave_summary" and event.available_codes == 2 for event in progress.events)
    assert progress.events[-1].stage == "finished"
    assert progress.events[-1].stop_reason == result.stop_reason


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
    progress = _Progress()
    observed_at = datetime(2026, 10, 16, 9, 15, tzinfo=SHANGHAI)

    result = DownloadRiskSnapshot(
        loader,
        ("600001",),
        RiskDownloadContext(date(2026, 10, 16), observed_at, lambda: 10.0, lambda: observed_at, progress),
        announcement_sync=announcement_sync,
    )

    assert loader.load_calls == []
    assert result.wave_count == 1
    assert result.stop_reason == "source_exhausted"
    assert result.changed_codes == 1
    assert result.official_pages_fetched == 3
    assert result.official_pages_checkpointed == 3
    assert result.official_announcements_saved == 42
    assert result.official_resumed_codes == 1
    assert result.official_failed_codes == 0
    assert loader.recovered_codes == [None, ("600001",)]
    assert any(
        event.stage == "official_announcements" and event.state == "running" and event.official_pending_codes == 0
        for event in progress.events
    )


def test_risk_download_waits_for_busy_daily_database_then_resumes_official_source() -> None:
    loader = _Loader()
    loader.cached_observations = {"600001": ResearchObservation(data_version="existing")}
    announcement_sync = _BusyThenReadyAnnouncementSync()
    progress = _Progress()
    observed_at = datetime(2026, 10, 16, 9, 15, tzinfo=SHANGHAI)

    result = DownloadRiskSnapshot(
        loader,
        ("600001",),
        RiskDownloadContext(date(2026, 10, 16), observed_at, lambda: 10.0, lambda: observed_at, progress),
        announcement_sync=announcement_sync,
    )

    assert announcement_sync.report_count == 2
    assert result.wave_count == 2
    assert result.official_pages_checkpointed == 3
    assert result.stop_reason != "no_progress"
    assert result.official_persistence_error is None
    assert any(event.official_persistence_error == "busy" for event in progress.events)


def test_risk_download_reports_non_lock_persistence_failure_without_retry_loop() -> None:
    loader = _Loader()
    loader.cached_observations = {"600001": ResearchObservation(data_version="existing")}
    observed_at = datetime(2026, 10, 16, 9, 15, tzinfo=SHANGHAI)

    result = DownloadRiskSnapshot(
        loader,
        ("600001",),
        RiskDownloadContext(date(2026, 10, 16), observed_at, lambda: 10.0, lambda: observed_at),
        announcement_sync=_UnavailableAnnouncementSync(),
    )

    assert result.wave_count == 1
    assert result.stop_reason == "persistence_unavailable"
    assert result.official_persistence_error == "unavailable"
    assert result.official_pages_checkpointed == 0


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


def test_risk_download_treats_official_page_checkpoint_as_durable_wave_progress() -> None:
    loader = _Loader()
    loader.cached_observations = {"600001": ResearchObservation(data_version="existing")}
    announcement_sync = _CheckpointAnnouncementSync()
    observed_at = datetime(2026, 10, 16, 9, 15, tzinfo=SHANGHAI)

    result = DownloadRiskSnapshot(
        loader,
        ("600001",),
        RiskDownloadContext(date(2026, 10, 16), observed_at, lambda: 10.0, lambda: observed_at),
        announcement_sync=announcement_sync,
    )

    assert announcement_sync.report_count == 2
    assert result.wave_count == 2
    assert result.official_pages_fetched == 2
    assert result.official_pages_checkpointed == 2
    assert result.official_resumed_codes == 1
    assert loader.recovered_codes == [None, ("600001",), ("600001",)]


def test_risk_download_prioritizes_unattempted_official_codes_across_waves() -> None:
    loader = _Loader()
    loader.cached_observations = {
        code: ResearchObservation(data_version="existing") for code in ("000001", "300001", "600001")
    }
    announcement_sync = _FairAnnouncementSync()
    observed_at = datetime(2026, 10, 16, 9, 15, tzinfo=SHANGHAI)

    result = DownloadRiskSnapshot(
        loader,
        ("600001", "000001", "300001"),
        RiskDownloadContext(date(2026, 10, 16), observed_at, lambda: 10.0, lambda: observed_at),
        announcement_sync=announcement_sync,
    )

    assert announcement_sync.sync_calls == [
        ("000001", "300001", "600001"),
        ("600001", "000001"),
        ("000001",),
    ]
    assert result.wave_count == 3
    assert result.stop_reason == "no_progress"
    assert result.deferred_codes == 1


def test_risk_download_stops_when_only_retryable_error_category_changes() -> None:
    loader = _OscillatingErrorLoader()
    progress = _Progress()
    observed_at = datetime(2026, 10, 16, 9, 15, tzinfo=SHANGHAI)

    result = DownloadRiskSnapshot(
        loader,
        ("600001",),
        RiskDownloadContext(
            date(2026, 10, 16),
            observed_at,
            lambda: 10.0,
            lambda: observed_at,
            progress=progress,
        ),
    )

    assert loader.attempts == 1
    assert result.wave_count == 1
    assert result.stop_reason == "no_progress"
    assert result.changed_codes == 0
    assert result.deferred_codes == 1
    assert any(event.stage == "research_retry" and event.state == "deferred" for event in progress.events)
    assert progress.events[-1].changed_codes == 0


def test_risk_download_prioritizes_previous_wave_deferred_retries() -> None:
    loader = _FairRetryLoader()
    observed_at = datetime(2026, 10, 16, 9, 15, tzinfo=SHANGHAI)

    result = DownloadRiskSnapshot(
        loader,
        ("600001", "000001", "300001"),
        RiskDownloadContext(date(2026, 10, 16), observed_at, lambda: 10.0, lambda: observed_at),
    )

    assert loader.load_calls == [
        (("000001", "300001", "600001"), True),
        (("600001", "000001"), True),
        (("000001",), True),
    ]
    assert result.wave_count == 3
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

    def execute(_path, *, progress):
        progress.publish(
            RiskDownloadProgress(
                stage="wave_summary",
                state="completed",
                wave=1,
                completed_units=80,
                total_units=100,
                requested_codes=100,
                available_codes=80,
                covered_codes=10,
                deferred_codes=20,
                changed_codes=20,
            )
        )
        return result

    monkeypatch.setattr("trader.bootstrap.ExecuteRiskDownload", execute)

    assert cli.Main(["--config", str(tmp_path / "runtime.json"), "risk_download"]) == 1

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert "risk_download 下载进度" in captured.err
    assert "波次汇总 | 完成" in captured.err
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
        "pages_checkpointed": 0,
        "announcements_saved": 0,
        "resumed_codes": 0,
        "failed_codes": 0,
        "persistence_error": None,
    }
    assert payload["elapsed_seconds"] == 123.457

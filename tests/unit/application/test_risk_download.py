from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from trader.recommendation.application.risk_download import DownloadRiskSnapshot, RiskDownloadContext
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
        return ResearchLoadReport(
            {codes[0]: ResearchObservation(data_version="risk:1")} if codes else {},
            changed_codes=(codes[0],) if codes else (),
            deferred_codes=(codes[1],) if len(codes) > 1 else (),
            deadline_reached=len(codes) > 1,
        )


def test_risk_download_reads_partition_before_bounded_supplier_refresh() -> None:
    loader = _Loader()
    observed_at = datetime(2026, 10, 16, 9, 15, tzinfo=SHANGHAI)
    ticks = iter((10.0, 15.5))

    result = DownloadRiskSnapshot(
        loader,
        ("600001", "000001"),
        RiskDownloadContext(date(2026, 10, 16), observed_at, lambda: next(ticks)),
    )

    assert loader.recovered_at == observed_at
    assert loader.retained_stale is True
    assert loader.deadline is not None
    assert (loader.deadline - observed_at).total_seconds() == 120.0
    assert result.requested_codes == 2
    assert result.available_codes == 0
    assert result.covered_codes == 0
    assert result.partial_codes == 0
    assert result.changed_codes == 1
    assert result.deferred_codes == 1
    assert result.failed_codes == 1
    assert result.deadline_reached
    assert result.needs_retry


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
    ticks = iter((10.0, 15.5))

    result = DownloadRiskSnapshot(
        loader,
        ("600001", "000001", "300001"),
        RiskDownloadContext(date(2026, 10, 16), observed_at, lambda: next(ticks)),
    )

    assert loader.loaded_codes == ("300001", "600001")
    assert loader.force is False
    assert result.available_codes == 1
    assert result.partial_codes == 1


def test_risk_download_retries_only_recoverable_component_errors_after_initial_coverage() -> None:
    loader = _Loader()
    loader.cached_observations = {
        "000001": ResearchObservation(source_errors=("pledge:schema_invalid",)),
        "300001": ResearchObservation(source_errors=("announcements:timeout",)),
        "600001": ResearchObservation(source_errors=("financial:resource_rejected",)),
        "600002": ResearchObservation(source_errors=("unlock:cancelled",)),
    }
    observed_at = datetime(2026, 10, 16, 9, 15, tzinfo=SHANGHAI)
    ticks = iter((10.0, 15.5))

    DownloadRiskSnapshot(
        loader,
        ("000001", "300001", "600001", "600002"),
        RiskDownloadContext(date(2026, 10, 16), observed_at, lambda: next(ticks)),
    )

    assert loader.loaded_codes == ("300001", "600001")
    assert loader.force is True

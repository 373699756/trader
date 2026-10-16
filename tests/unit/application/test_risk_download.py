from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from trader.recommendation.application.risk_download import DownloadRiskSnapshot
from trader.recommendation.domain.market.research import ResearchObservation
from trader.recommendation.infra.market_data.research_load_status import ResearchLoadReport

SHANGHAI = ZoneInfo("Asia/Shanghai")


class _Loader:
    def __init__(self) -> None:
        self.recovered_at: datetime | None = None
        self.deadline: datetime | None = None

    def recover_from_data_plane(self, observed_at: datetime | None = None) -> None:
        self.recovered_at = observed_at

    def load_report(self, codes, observed_at, **options):
        self.deadline = options["deadline"]
        return ResearchLoadReport(
            {codes[0]: ResearchObservation(data_version="risk:1")},
            changed_codes=(codes[0],),
            deferred_codes=(codes[1],),
            deadline_reached=True,
        )


def test_risk_download_reads_partition_before_bounded_supplier_refresh() -> None:
    loader = _Loader()
    observed_at = datetime(2026, 10, 16, 9, 15, tzinfo=SHANGHAI)
    ticks = iter((10.0, 15.5))

    result = DownloadRiskSnapshot(
        loader,
        ("600001", "000001"),
        target_date=date(2026, 10, 16),
        observed_at=observed_at,
        monotonic=lambda: next(ticks),
    )

    assert loader.recovered_at == observed_at
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

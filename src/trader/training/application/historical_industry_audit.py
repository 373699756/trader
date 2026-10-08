"""Read historical industry evidence from its single typed owner."""

from typing import Protocol

from trader.training.domain.evaluation.historical_industry_facts import HistoricalIndustryDatasetReport


class HistoricalIndustryEvidencePort(Protocol):
    def audit(self, *, required_sample_codes: int, tushare_access_points: int) -> HistoricalIndustryDatasetReport: ...


class AuditHistoricalIndustry:
    def __init__(self, evidence: HistoricalIndustryEvidencePort) -> None:
        self._evidence = evidence

    def execute(self, *, required_sample_codes: int, tushare_access_points: int) -> HistoricalIndustryDatasetReport:
        return self._evidence.audit(
            required_sample_codes=required_sample_codes, tushare_access_points=tushare_access_points
        )

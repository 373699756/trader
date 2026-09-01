from datetime import date, timedelta

from trader.application.research.historical_label import HistoricalLabelPreregistrationService
from trader.domain.research.historical_label import H1CoverageMetadata


class _MetadataPort:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def label_metadata(self, spec):
        self.calls.append(spec.strategy)
        dates = tuple(date(2022, 1, 1) + timedelta(days=index) for index in range(1_000))
        return H1CoverageMetadata(spec.strategy, "coverage_ready", dates, "a" * 64, "b" * 64, spec.source_cutoff)


def test_preregistration_service_reads_only_each_strategy_h1_metadata() -> None:
    port = _MetadataPort()

    batch = HistoricalLabelPreregistrationService(port).execute()

    assert port.calls == ["today", "tomorrow", "d25"]
    assert tuple(item.status for item in batch.strategies) == ("preregistered",) * 3
    assert all(item.terminal_holdout_status == "terminal_holdout_not_opened" for item in batch.strategies)
    assert all(item.candidate_results_generated is False for item in batch.strategies)

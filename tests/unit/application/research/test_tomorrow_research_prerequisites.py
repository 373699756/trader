from datetime import date, timedelta

from trader.application.research.historical_label import HistoricalLabelPreregistrationService
from trader.application.research.tomorrow_research_prerequisites import CodexATomorrowResearchPrerequisite
from trader.domain.research.h1_point_in_time import H1PointInTimeSpec
from trader.domain.research.historical_label import H1CoverageMetadata


class _MetadataPort:
    def __init__(self, *, ready: bool) -> None:
        self._ready = ready

    def label_metadata(self, spec: H1PointInTimeSpec) -> H1CoverageMetadata:
        dates = tuple(date(2020, 1, 1) + timedelta(days=index) for index in range(1_000)) if self._ready else ()
        return H1CoverageMetadata(
            spec.strategy,
            "coverage_ready" if self._ready else "historical_data_insufficient",
            dates,
            "a" * 64,
            "b" * 64,
            spec.source_cutoff,
        )


def test_codex_a_prerequisite_blocks_on_tomorrow_h1_metadata_without_sealing_artifacts() -> None:
    prerequisite = CodexATomorrowResearchPrerequisite(
        HistoricalLabelPreregistrationService(_MetadataPort(ready=False))
    ).inspect()

    assert prerequisite.status == "blocked"
    assert prerequisite.blockers == (
        "tomorrow_common_trading_days_below_1000",
        "tomorrow_h1_historical_data_insufficient",
        "tomorrow_terminal_holdout_below_200",
    )
    assert prerequisite.production_authority is False


def test_codex_a_prerequisite_releases_resource_probe_only_after_preregistration_is_ready() -> None:
    prerequisite = CodexATomorrowResearchPrerequisite(
        HistoricalLabelPreregistrationService(_MetadataPort(ready=True))
    ).inspect()

    assert prerequisite.status == "ready"
    assert prerequisite.blockers == ()
    assert len(prerequisite.prerequisite_hash) == 64
    assert len(prerequisite.content_hash) == 64

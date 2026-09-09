from __future__ import annotations

from datetime import date

from trader.application.research.limited_factor_family import LimitedFactorFamilyResearchBuilder
from trader.domain.research.candidate_recall_ledger import CandidateRecallReport
from trader.domain.research.limited_factor_family import (
    LimitedFactorCandidate,
    LimitedFactorFamilySpec,
)
from trader.domain.research.point_in_time_dataset import PointInTimeDatasetReport

HASH = "a" * 64


class _NeverCalledSource:
    def __init__(self) -> None:
        self.calls = 0

    def load_candidate_series(self, spec, dataset, recall):  # type: ignore[no-untyped-def]
        self.calls += 1
        raise AssertionError("factor evidence source must not be read")


def _spec() -> LimitedFactorFamilySpec:
    dates = tuple(date(2024, 1, day) for day in range(1, 11))
    return LimitedFactorFamilySpec(
        family_id="intraday_price_volume_path",
        control_feature_ids=("a", "b", "c", "d", "e", "f"),
        candidates=(
            LimitedFactorCandidate("existing_six_alpha", "control", "unitless", "control"),
            LimitedFactorCandidate("tail_volume_share", "tail_volume_share", "ratio"),
        ),
        selected_candidate_id="tail_volume_share",
        development_dates=dates[:5],
        confirmation_dates=dates[5:],
    )


def test_builder_does_not_read_factor_source_when_point_in_time_data_is_insufficient() -> None:
    dataset = PointInTimeDatasetReport(HASH, "historical_data_insufficient", (), None, ("source_unavailable",))
    recall = CandidateRecallReport(
        "historical_data_insufficient",
        dataset.content_hash,
        None,
        None,
        (),
        (),
        ("point_in_time_dataset_unavailable",),
    )
    source = _NeverCalledSource()

    report = LimitedFactorFamilyResearchBuilder(source).build(_spec(), dataset, recall)

    assert report.state == "historical_data_insufficient"
    assert report.failure_reasons == ("point_in_time_dataset_unavailable",)
    assert source.calls == 0


def test_builder_rejects_recall_report_from_another_dataset_without_source_reads() -> None:
    dataset = PointInTimeDatasetReport(HASH, "historical_data_insufficient", (), None, ("source_unavailable",))
    recall = CandidateRecallReport(
        "historical_data_insufficient",
        "b" * 64,
        None,
        None,
        (),
        (),
        ("point_in_time_dataset_unavailable",),
    )
    source = _NeverCalledSource()

    report = LimitedFactorFamilyResearchBuilder(source).build(_spec(), dataset, recall)

    assert report.failure_reasons == ("candidate_recall_parent_mismatch",)
    assert source.calls == 0

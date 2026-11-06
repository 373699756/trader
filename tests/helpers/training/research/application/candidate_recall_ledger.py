from __future__ import annotations
from collections import Counter
from datetime import date, datetime, timedelta
from typing import cast
from zoneinfo import ZoneInfo
from trader.recommendation.domain.market.feature_contracts import FeatureVector
from trader.recommendation.domain.market.models import Board
from trader.recommendation.domain.publication.models import Strategy
from trader.training.domain.evaluation.candidate_recall_ledger import CANDIDATE_RECALL_STAGES, CandidateRecallDayTrace, CandidateRecallDownstreamBoundary, CandidateRecallDownstreamTrace, CandidateRecallStageLatency
from trader.recommendation.domain.outcomes.models import OutcomeExitStatus, RecommendationOutcome
from trader.training.domain.evaluation.point_in_time_dataset import (
    POINT_IN_TIME_BOUNDARIES,
    PointInTimeBoardPopulation,
    PointInTimeBoundaryCount,
    PointInTimeCostOutcome,
    PointInTimeCoverage,
    PointInTimeDatasetManifest,
    PointInTimeDatasetReport,
    PointInTimeDatasetRow,
    PointInTimeDateSplit,
    PointInTimeDayDataset,
    PointInTimeIndustryFact,
    PointInTimePartitionManifest,
    PointInTimeRejectionBoundary,
    PointInTimeSourceIdentity,
)

HASHES = tuple(character * 64 for character in "abcdef0123456789")

SHANGHAI = ZoneInfo("Asia/Shanghai")

FEATURE_HASH = HASHES[0]


def _split() -> PointInTimeDateSplit:
    start = date(2024, 1, 1)
    return PointInTimeDateSplit(
        training_dates=(start,),
        early_stopping_dates=(start + timedelta(days=1),),
        calibration_dates=(start + timedelta(days=2),),
        development_confirmation_embargo_dates=tuple(start + timedelta(days=3 + index) for index in range(5)),
        confirmation_dates=(start + timedelta(days=8),),
        confirmation_holdout_embargo_dates=tuple(start + timedelta(days=9 + index) for index in range(5)),
        terminal_holdout_dates=tuple(start + timedelta(days=14 + index) for index in range(200)),
    )


def _outcomes(trade_date: date, index: int, net_offset: float = 0.0) -> tuple[PointInTimeCostOutcome, ...]:
    anchor = datetime(trade_date.year, trade_date.month, trade_date.day, 15, 0, tzinfo=SHANGHAI)
    desired_net_20bp = 60.0 - index - net_offset
    gross = desired_net_20bp + 0.2
    severe = index in {9, 10}
    untradable = index == 9
    exit_date = (trade_date + timedelta(days=1)).isoformat()
    return tuple(
        PointInTimeCostOutcome(
            cost_bps,
            RecommendationOutcome(
                snapshot_id=f"point-in-time:{trade_date.isoformat()}:{600000 + index:06d}",
                strategy=Strategy.TOMORROW,
                recommend_date=trade_date.isoformat(),
                stock_code=f"{600000 + index:06d}",
                horizon=1,
                status="complete",
                settled_at=anchor + timedelta(days=2),
                anchor_raw_price=10.0,
                anchor_qfq_price=10.0,
                atr20_pct=2.0,
                minimum_qfq_low=9.6 if severe else 9.9,
                end_qfq_close=10.0 * (1.0 + gross / 100.0),
                exit_status=OutcomeExitStatus.SUSPENDED if untradable else OutcomeExitStatus.TRADABLE,
                untradable_dates=(exit_date,) if untradable else (),
                gross_return_pct=gross,
                benchmark_return_pct=0.0,
                net_excess_return_pct=gross - cost_bps / 100.0,
                mae_pct=-4.0 if severe else -1.0,
                mae_atr=-2.0 if severe else -0.5,
                severe_drawdown=severe,
            ),
        )
        for cost_bps in (20, 50, 100)
    )


def _point_boundary(index: int) -> PointInTimeRejectionBoundary:
    return cast(
        PointInTimeRejectionBoundary,
        {
            0: "permanent_eligibility",
            1: "dynamic_hard_filter",
            2: "field_eligibility",
            3: "candidate_threshold",
            4: "board_limit",
        }.get(index, "eligible"),
    )


def _day(trade_date: date, population_size: int = 60, net_offset: float = 0.0) -> PointInTimeDayDataset:
    anchor = datetime(trade_date.year, trade_date.month, trade_date.day, 15, 0, tzinfo=SHANGHAI)
    rows = []
    for index in range(population_size):
        code = f"{600000 + index:06d}"
        board = (Board.MAIN, Board.CHINEXT, Board.STAR)[index % 3]
        boundary = _point_boundary(index)
        rows.append(
            PointInTimeDatasetRow(
                code=code,
                trade_date=trade_date,
                anchor_at=anchor,
                board=board,
                industry=f"industry-{index % 4}",
                anchor_raw_price=10.0,
                feature_vector=FeatureVector(FEATURE_HASH, (float(index),), (False,)),
                source_identity=PointInTimeSourceIdentity(
                    daily_path_hash=HASHES[1],
                    minute_path_hash=HASHES[2],
                    security_fact_hash=HASHES[3],
                    industry_fact_hash=HASHES[4],
                    event_fact_hash=HASHES[5],
                ),
                industry_fact=PointInTimeIndustryFact(
                    code=code,
                    industry=f"industry-{index % 4}",
                    classification="official_classification",
                    effective_from=trade_date - timedelta(days=365),
                    effective_to=None,
                    queried_at=anchor + timedelta(days=365),
                    source="official_industry",
                    content_hash=HASHES[4],
                ),
                event_facts=(),
                first_rejection_boundary=boundary,
                rejection_reasons=() if boundary == "eligible" else (boundary,),
                benchmark_eligible=boundary in {"eligible", "candidate_threshold", "board_limit"},
                candidate_eligible=boundary == "eligible",
                candidate_score=None if index < 3 else 100.0 - index,
                candidate_rank=0 if index < 3 else index - 2,
                outcomes=_outcomes(trade_date, index, net_offset),
            )
        )
    counts = Counter(row.first_rejection_boundary for row in rows)
    coverage = PointInTimeCoverage(
        total_rows=len(rows),
        benchmark_eligible_rows=sum(row.benchmark_eligible for row in rows),
        candidate_eligible_rows=sum(row.candidate_eligible for row in rows),
        label_complete_rows=sum(row.label_complete for row in rows),
        boundary_counts=tuple(
            PointInTimeBoundaryCount(boundary, counts[boundary]) for boundary in POINT_IN_TIME_BOUNDARIES
        ),
    )
    populations = tuple(
        PointInTimeBoardPopulation(
            board,
            f"population-{board.value}",
            sum(row.benchmark_eligible and row.board is board for row in rows),
        )
        for board in (Board.MAIN, Board.CHINEXT, Board.STAR)
    )
    return PointInTimeDayDataset(trade_date, anchor, tuple(rows), coverage, populations)


def _dataset(population_sizes: tuple[int, ...] = (60, 60, 60, 60), net_offset: float = 0.0) -> PointInTimeDatasetReport:
    split = _split()
    dates = split.development_dates
    days = tuple(_day(item, size, net_offset) for item, size in zip(dates, population_sizes, strict=True))
    partitions = tuple(
        PointInTimePartitionManifest(name, (trade_date,), (day.content_hash,), len(day.rows))
        for name, trade_date, day in zip(
            ("training", "early_stopping", "calibration", "confirmation"),
            dates,
            days,
            strict=True,
        )
    )
    manifest = PointInTimeDatasetManifest(
        qualification_hash=HASHES[6],
        daily_archive_manifest_hash=HASHES[7],
        feature_manifest_hash=FEATURE_HASH,
        calendar_hash=HASHES[8],
        security_master_hash=HASHES[9],
        selection_policy_hash=HASHES[10],
        date_split=split,
        date_split_hash=split.content_hash,
        partitions=partitions,
        total_rows=sum(len(day.rows) for day in days),
    )
    return PointInTimeDatasetReport(
        qualification_hash=HASHES[6],
        state="historical_point_in_time_parity",
        days=days,
        manifest=manifest,
        failure_reasons=(),
    )


def _trace(day: PointInTimeDayDataset) -> CandidateRecallDayTrace:
    downstream = []
    for index, row in enumerate(day.rows):
        if not row.candidate_eligible:
            continue
        boundary = {5: "scoring", 6: "risk", 7: "action", 8: "concentration"}.get(index, "selected")
        downstream.append(
            CandidateRecallDownstreamTrace(
                trade_date=day.trade_date,
                code=row.code,
                dataset_row_hash=row.content_hash,
                first_rejection_boundary=cast(CandidateRecallDownstreamBoundary, boundary),
                rejection_reasons=() if boundary == "selected" else (f"{boundary}_rejected",),
            )
        )
    latencies = tuple(
        CandidateRecallStageLatency(stage, float(index)) for index, stage in enumerate(CANDIDATE_RECALL_STAGES)
    )
    return CandidateRecallDayTrace(day.trade_date, day.content_hash, HASHES[11], tuple(downstream), latencies)


class _TraceSource:
    def __init__(self, dataset: PointInTimeDatasetReport) -> None:
        self.traces = {day.trade_date: _trace(day) for day in dataset.days}
        self.loaded: list[date] = []

    def load_day_trace(self, trade_date: date, dataset_day_hash: str) -> CandidateRecallDayTrace | None:
        self.loaded.append(trade_date)
        trace = self.traces.get(trade_date)
        assert trace is None or trace.dataset_day_hash == dataset_day_hash
        return trace


class _ForbiddenTraceSource:
    def load_day_trace(self, trade_date: date, dataset_day_hash: str) -> CandidateRecallDayTrace | None:
        raise AssertionError(f"trace source must not be read: {trade_date}:{dataset_day_hash}")


class _ForbiddenFactorSource:
    def load_candidate_series(self, spec, dataset, recall):  # type: ignore[no-untyped-def]
        raise AssertionError("factor outcomes must not be read for invalid recall evidence")


def _insufficient_dataset() -> PointInTimeDatasetReport:
    return PointInTimeDatasetReport(
        qualification_hash=HASHES[6],
        state="historical_data_insufficient",
        days=(),
        manifest=None,
        failure_reasons=("point_in_time_data_not_qualified",),
    )

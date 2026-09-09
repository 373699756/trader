"""Immutable offline candidate-funnel recall attribution."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Literal, cast

from trader.domain.market.models import Board
from trader.domain.research.h1_point_in_time import canonical_hash
from trader.domain.research.point_in_time_dataset import (
    PointInTimeDatasetReport,
    PointInTimeDatasetRow,
    PointInTimeDayDataset,
)

CandidateRecallState = Literal["candidate_recall_attributed", "historical_data_insufficient"]
CandidateRecallStage = Literal[
    "population",
    "permanent_eligibility",
    "dynamic_hard_filter",
    "field_eligibility",
    "candidate_threshold",
    "board_limit",
    "scoring",
    "risk",
    "action",
    "concentration",
]
CandidateRecallBoundary = Literal[
    "permanent_eligibility",
    "dynamic_hard_filter",
    "field_eligibility",
    "candidate_threshold",
    "board_limit",
    "scoring",
    "risk",
    "action",
    "concentration",
    "selected",
]
CandidateRecallDownstreamBoundary = Literal["scoring", "risk", "action", "concentration", "selected"]

CANDIDATE_RECALL_TOP_K = (10, 20, 50)
CANDIDATE_RECALL_STAGES: tuple[CandidateRecallStage, ...] = (
    "population",
    "permanent_eligibility",
    "dynamic_hard_filter",
    "field_eligibility",
    "candidate_threshold",
    "board_limit",
    "scoring",
    "risk",
    "action",
    "concentration",
)
_SUPPORTED_BOARDS = (Board.MAIN, Board.CHINEXT, Board.STAR)
_HASH = re.compile(r"^[0-9a-f]{64}$")
_REASON = re.compile(r"^[a-z0-9_]{1,96}$")
_BOUNDARY_INDEX = {boundary: index for index, boundary in enumerate(CANDIDATE_RECALL_STAGES)}


class CandidateRecallTraceMismatchError(ValueError):
    """Raised when funnel trace evidence does not bind to its dataset day."""


@dataclass(frozen=True)
class CandidateRecallStageLatency:
    stage: CandidateRecallStage
    cumulative_latency_ms: float

    def __post_init__(self) -> None:
        if self.stage not in CANDIDATE_RECALL_STAGES:
            raise ValueError("candidate recall latency stage is invalid")
        if not math.isfinite(self.cumulative_latency_ms) or self.cumulative_latency_ms < 0.0:
            raise ValueError("candidate recall latency must be finite and non-negative")


@dataclass(frozen=True)
class CandidateRecallDownstreamTrace:
    trade_date: date
    code: str
    dataset_row_hash: str
    first_rejection_boundary: CandidateRecallDownstreamBoundary
    rejection_reasons: tuple[str, ...]
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        reasons = tuple(dict.fromkeys(self.rejection_reasons))
        if len(self.code) != 6 or not self.code.isdigit() or _HASH.fullmatch(self.dataset_row_hash) is None:
            raise ValueError("candidate recall downstream trace identity is invalid")
        if self.first_rejection_boundary not in {"scoring", "risk", "action", "concentration", "selected"}:
            raise ValueError("candidate recall downstream boundary is invalid")
        if any(_REASON.fullmatch(item) is None for item in reasons):
            raise ValueError("candidate recall rejection reasons are invalid")
        if (self.first_rejection_boundary == "selected") == bool(reasons):
            raise ValueError("candidate recall boundary and reasons are inconsistent")
        object.__setattr__(self, "rejection_reasons", reasons)
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class CandidateRecallDayTrace:
    trade_date: date
    dataset_day_hash: str
    latency_evidence_hash: str
    downstream_rows: tuple[CandidateRecallDownstreamTrace, ...]
    stage_latencies: tuple[CandidateRecallStageLatency, ...]
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        rows = tuple(sorted(self.downstream_rows, key=lambda item: item.code))
        latencies = tuple(self.stage_latencies)
        if any(_HASH.fullmatch(value) is None for value in (self.dataset_day_hash, self.latency_evidence_hash)):
            raise ValueError("candidate recall day trace identity is invalid")
        if len({item.code for item in rows}) != len(rows) or any(item.trade_date != self.trade_date for item in rows):
            raise ValueError("candidate recall downstream rows are invalid")
        if tuple(item.stage for item in latencies) != CANDIDATE_RECALL_STAGES:
            raise ValueError("candidate recall latencies must use the fixed order")
        values = tuple(item.cumulative_latency_ms for item in latencies)
        if values != tuple(sorted(values)):
            raise ValueError("candidate recall cumulative latency must be monotonic")
        object.__setattr__(self, "downstream_rows", rows)
        object.__setattr__(self, "stage_latencies", latencies)
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class CandidateRecallLedgerRow:
    trade_date: date
    code: str
    board: Board
    dataset_row_hash: str
    first_rejection_boundary: CandidateRecallBoundary
    rejection_reasons: tuple[str, ...]
    candidate_rank: int
    oracle_rank: int
    net_excess_return_20bp: float
    exit_untradable: bool
    severe_loss: bool
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        reasons = tuple(dict.fromkeys(self.rejection_reasons))
        if (
            len(self.code) != 6
            or not self.code.isdigit()
            or self.board not in _SUPPORTED_BOARDS
            or _HASH.fullmatch(self.dataset_row_hash) is None
        ):
            raise ValueError("candidate recall ledger row identity is invalid")
        if self.first_rejection_boundary not in {*CANDIDATE_RECALL_STAGES[1:], "selected"}:
            raise ValueError("candidate recall ledger row boundary is invalid")
        if any(_REASON.fullmatch(item) is None for item in reasons):
            raise ValueError("candidate recall ledger row reasons are invalid")
        if (self.first_rejection_boundary == "selected") == bool(reasons):
            raise ValueError("candidate recall ledger row boundary and reasons are inconsistent")
        if self.candidate_rank < 0 or self.oracle_rank < 1:
            raise ValueError("candidate recall ledger ranks are invalid")
        if not math.isfinite(self.net_excess_return_20bp):
            raise ValueError("candidate recall ledger outcome is invalid")
        object.__setattr__(self, "rejection_reasons", reasons)
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class CandidateRecallBoardRecall:
    board: Board
    oracle_count: int
    recalled_count: int
    recall: float | None

    def __post_init__(self) -> None:
        if self.board not in _SUPPORTED_BOARDS:
            raise ValueError("candidate recall board is invalid")
        _validate_recall(self.oracle_count, self.recalled_count, self.recall, "candidate recall board")


@dataclass(frozen=True)
class CandidateRecallTopKMetric:
    top_k: int
    oracle_count: int
    recalled_count: int
    recall: float | None
    board_recalls: tuple[CandidateRecallBoardRecall, ...]

    def __post_init__(self) -> None:
        boards = tuple(self.board_recalls)
        if self.top_k not in CANDIDATE_RECALL_TOP_K:
            raise ValueError("candidate recall TopK is invalid")
        _validate_recall(self.oracle_count, self.recalled_count, self.recall, "candidate recall TopK")
        if tuple(item.board for item in boards) != _SUPPORTED_BOARDS:
            raise ValueError("candidate recall board metrics must use the fixed order")
        if (
            sum(item.oracle_count for item in boards) != self.oracle_count
            or sum(item.recalled_count for item in boards) != self.recalled_count
        ):
            raise ValueError("candidate recall board metrics do not match TopK")
        object.__setattr__(self, "board_recalls", boards)


@dataclass(frozen=True)
class CandidateRecallDayStageMetrics:
    stage: CandidateRecallStage
    retained_count: int
    top_k_metrics: tuple[CandidateRecallTopKMetric, ...]
    exit_untradable_rate: float | None
    severe_loss_rate: float | None
    cumulative_latency_ms: float

    def __post_init__(self) -> None:
        if self.stage not in CANDIDATE_RECALL_STAGES or self.retained_count < 0:
            raise ValueError("candidate recall day stage identity is invalid")
        if tuple(item.top_k for item in self.top_k_metrics) != CANDIDATE_RECALL_TOP_K:
            raise ValueError("candidate recall day stage requires fixed TopK metrics")
        _validate_rates((self.exit_untradable_rate, self.severe_loss_rate), "candidate recall day stage")
        if self.retained_count == 0 and any(
            value is not None for value in (self.exit_untradable_rate, self.severe_loss_rate)
        ):
            raise ValueError("empty candidate recall day stage cannot expose outcome rates")
        if not math.isfinite(self.cumulative_latency_ms) or self.cumulative_latency_ms < 0.0:
            raise ValueError("candidate recall day stage latency is invalid")


@dataclass(frozen=True)
class CandidateRecallDayAttribution:
    trade_date: date
    dataset_day_hash: str
    trace_hash: str
    latency_evidence_hash: str
    rows: tuple[CandidateRecallLedgerRow, ...]
    stages: tuple[CandidateRecallDayStageMetrics, ...]
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        rows = tuple(sorted(self.rows, key=lambda item: item.code))
        stages = tuple(self.stages)
        if any(
            _HASH.fullmatch(value) is None
            for value in (self.dataset_day_hash, self.trace_hash, self.latency_evidence_hash)
        ):
            raise ValueError("candidate recall day attribution identity is invalid")
        if not rows or len({item.code for item in rows}) != len(rows):
            raise ValueError("candidate recall day attribution requires unique rows")
        if any(item.trade_date != self.trade_date for item in rows):
            raise ValueError("candidate recall day attribution row date is invalid")
        if tuple(sorted(item.oracle_rank for item in rows)) != tuple(range(1, len(rows) + 1)):
            raise ValueError("candidate recall oracle ranks must be contiguous")
        actual_oracle_order = tuple(item.code for item in sorted(rows, key=lambda item: item.oracle_rank))
        expected_oracle_order = tuple(
            item.code for item in sorted(rows, key=lambda item: (-item.net_excess_return_20bp, item.code))
        )
        if actual_oracle_order != expected_oracle_order:
            raise ValueError("candidate recall oracle order is inconsistent")
        if tuple(item.stage for item in stages) != CANDIDATE_RECALL_STAGES:
            raise ValueError("candidate recall day stages must use the fixed order")
        expected_stages = tuple(_day_stage(item.stage, rows, item.cumulative_latency_ms) for item in stages)
        if stages != expected_stages:
            raise ValueError("candidate recall day stage metrics do not match rows")
        object.__setattr__(self, "rows", rows)
        object.__setattr__(self, "stages", stages)
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class CandidateRecallAggregateStageMetrics:
    stage: CandidateRecallStage
    day_count: int
    total_retained_count: int
    mean_retained_count: float
    top_k_metrics: tuple[CandidateRecallTopKMetric, ...]
    exit_untradable_rate: float | None
    severe_loss_rate: float | None
    p50_cumulative_latency_ms: float
    p95_cumulative_latency_ms: float

    def __post_init__(self) -> None:
        values = (
            self.mean_retained_count,
            self.p50_cumulative_latency_ms,
            self.p95_cumulative_latency_ms,
        )
        if (
            self.stage not in CANDIDATE_RECALL_STAGES
            or self.day_count < 1
            or self.total_retained_count < 0
            or any(not math.isfinite(value) or value < 0.0 for value in values)
            or not math.isclose(self.mean_retained_count, self.total_retained_count / self.day_count)
            or self.p95_cumulative_latency_ms < self.p50_cumulative_latency_ms
        ):
            raise ValueError("candidate recall aggregate stage identity is invalid")
        if tuple(item.top_k for item in self.top_k_metrics) != CANDIDATE_RECALL_TOP_K:
            raise ValueError("candidate recall aggregate stage requires fixed TopK metrics")
        _validate_rates((self.exit_untradable_rate, self.severe_loss_rate), "candidate recall aggregate stage")
        if self.total_retained_count == 0 and any(
            value is not None for value in (self.exit_untradable_rate, self.severe_loss_rate)
        ):
            raise ValueError("empty candidate recall aggregate stage cannot expose outcome rates")


@dataclass(frozen=True)
class CandidateRecallReport:
    state: CandidateRecallState
    dataset_report_hash: str
    dataset_manifest_hash: str | None
    latency_evidence_hash: str | None
    days: tuple[CandidateRecallDayAttribution, ...]
    aggregate_stages: tuple[CandidateRecallAggregateStageMetrics, ...]
    failure_reasons: tuple[str, ...]
    terminal_holdout_opened: bool = False
    production_authority: bool = False
    schema_version: str = "candidate_recall_report"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        days = tuple(sorted(self.days, key=lambda item: item.trade_date))
        stages = tuple(self.aggregate_stages)
        reasons = tuple(sorted(set(self.failure_reasons)))
        if (
            _HASH.fullmatch(self.dataset_report_hash) is None
            or (self.dataset_manifest_hash is not None and _HASH.fullmatch(self.dataset_manifest_hash) is None)
            or (self.latency_evidence_hash is not None and _HASH.fullmatch(self.latency_evidence_hash) is None)
            or any(_REASON.fullmatch(item) is None for item in reasons)
        ):
            raise ValueError("candidate recall report identity is invalid")
        if self.state == "candidate_recall_attributed":
            if (
                self.dataset_manifest_hash is None
                or self.latency_evidence_hash is None
                or not days
                or len({item.trade_date for item in days}) != len(days)
                or any(item.latency_evidence_hash != self.latency_evidence_hash for item in days)
                or reasons
                or tuple(item.stage for item in stages) != CANDIDATE_RECALL_STAGES
            ):
                raise ValueError("candidate recall attributed report is incomplete")
            expected_stages = tuple(_aggregate_stage(stage, days) for stage in CANDIDATE_RECALL_STAGES)
            if stages != expected_stages:
                raise ValueError("candidate recall aggregate stages do not match days")
        elif self.state == "historical_data_insufficient":
            if self.latency_evidence_hash is not None or days or stages or not reasons:
                raise ValueError("insufficient candidate recall report must fail closed")
        else:
            raise ValueError("candidate recall report state is invalid")
        if self.terminal_holdout_opened or self.production_authority:
            raise ValueError("candidate recall report cannot open holdout or production")
        if self.schema_version != "candidate_recall_report":
            raise ValueError("candidate recall report schema is invalid")
        object.__setattr__(self, "days", days)
        object.__setattr__(self, "aggregate_stages", stages)
        object.__setattr__(self, "failure_reasons", reasons)
        object.__setattr__(self, "content_hash", canonical_hash(self))


def attribute_candidate_recall_day(
    day: PointInTimeDayDataset,
    trace: CandidateRecallDayTrace,
) -> CandidateRecallDayAttribution:
    if trace.trade_date != day.trade_date or trace.dataset_day_hash != day.content_hash:
        raise CandidateRecallTraceMismatchError("candidate recall trace does not match the dataset day")
    expected_codes = {row.code for row in day.rows if row.candidate_eligible}
    trace_by_code = {row.code: row for row in trace.downstream_rows}
    if set(trace_by_code) != expected_codes:
        raise CandidateRecallTraceMismatchError("candidate recall downstream trace coverage is incomplete")
    by_code = {row.code: row for row in day.rows}
    if any(trace_by_code[code].dataset_row_hash != by_code[code].content_hash for code in expected_codes):
        raise CandidateRecallTraceMismatchError("candidate recall downstream trace row hash is invalid")
    ranked = sorted(day.rows, key=lambda row: (-_outcome_20bp(row)[0], row.code))
    rank_by_code = {row.code: rank for rank, row in enumerate(ranked, start=1)}
    rows = tuple(_ledger_row(row, trace_by_code.get(row.code), rank_by_code[row.code]) for row in day.rows)
    latency_by_stage = {item.stage: item.cumulative_latency_ms for item in trace.stage_latencies}
    stages = tuple(_day_stage(stage, rows, latency_by_stage[stage]) for stage in CANDIDATE_RECALL_STAGES)
    return CandidateRecallDayAttribution(
        day.trade_date,
        day.content_hash,
        trace.content_hash,
        trace.latency_evidence_hash,
        rows,
        stages,
    )


def build_candidate_recall_report(
    dataset: PointInTimeDatasetReport,
    days: tuple[CandidateRecallDayAttribution, ...],
) -> CandidateRecallReport:
    if dataset.state != "historical_point_in_time_parity" or dataset.manifest is None:
        return candidate_recall_insufficient_report(dataset, "point_in_time_dataset_unavailable")
    ordered = tuple(sorted(days, key=lambda item: item.trade_date))
    if tuple((item.trade_date, item.dataset_day_hash) for item in ordered) != tuple(
        (item.trade_date, item.content_hash) for item in dataset.days
    ):
        raise CandidateRecallTraceMismatchError("candidate recall attributed days do not match the dataset")
    latency_evidence_hashes = {item.latency_evidence_hash for item in ordered}
    if len(latency_evidence_hashes) != 1:
        raise CandidateRecallTraceMismatchError("candidate recall latency evidence identity is inconsistent")
    aggregates = tuple(_aggregate_stage(stage, ordered) for stage in CANDIDATE_RECALL_STAGES)
    return CandidateRecallReport(
        state="candidate_recall_attributed",
        dataset_report_hash=dataset.content_hash,
        dataset_manifest_hash=dataset.manifest.content_hash,
        latency_evidence_hash=next(iter(latency_evidence_hashes)),
        days=ordered,
        aggregate_stages=aggregates,
        failure_reasons=(),
    )


def candidate_recall_insufficient_report(
    dataset: PointInTimeDatasetReport,
    reason: str,
) -> CandidateRecallReport:
    return CandidateRecallReport(
        state="historical_data_insufficient",
        dataset_report_hash=dataset.content_hash,
        dataset_manifest_hash=dataset.manifest.content_hash if dataset.manifest is not None else None,
        latency_evidence_hash=None,
        days=(),
        aggregate_stages=(),
        failure_reasons=(reason,),
    )


def _ledger_row(
    row: PointInTimeDatasetRow,
    trace: CandidateRecallDownstreamTrace | None,
    oracle_rank: int,
) -> CandidateRecallLedgerRow:
    net, exit_untradable, severe_loss = _outcome_20bp(row)
    if row.first_rejection_boundary == "eligible":
        if trace is None:
            raise CandidateRecallTraceMismatchError("candidate recall downstream trace is missing")
        boundary: CandidateRecallBoundary = trace.first_rejection_boundary
        reasons = trace.rejection_reasons
    else:
        if trace is not None:
            raise CandidateRecallTraceMismatchError("rejected dataset row cannot have downstream trace")
        boundary = cast(CandidateRecallBoundary, row.first_rejection_boundary)
        reasons = row.rejection_reasons
    return CandidateRecallLedgerRow(
        trade_date=row.trade_date,
        code=row.code,
        board=row.board,
        dataset_row_hash=row.content_hash,
        first_rejection_boundary=boundary,
        rejection_reasons=reasons,
        candidate_rank=row.candidate_rank,
        oracle_rank=oracle_rank,
        net_excess_return_20bp=net,
        exit_untradable=exit_untradable,
        severe_loss=severe_loss,
    )


def _outcome_20bp(row: PointInTimeDatasetRow) -> tuple[float, bool, bool]:
    outcome = next((item.outcome for item in row.outcomes if item.cost_bps == 20), None)
    if (
        outcome is None
        or outcome.status != "complete"
        or outcome.net_excess_return_pct is None
        or outcome.exit_untradable is None
        or outcome.severe_drawdown is None
    ):
        raise CandidateRecallTraceMismatchError("candidate recall requires complete 20bp outcomes")
    return outcome.net_excess_return_pct, outcome.exit_untradable, outcome.severe_drawdown


def _day_stage(
    stage: CandidateRecallStage,
    rows: tuple[CandidateRecallLedgerRow, ...],
    cumulative_latency_ms: float,
) -> CandidateRecallDayStageMetrics:
    retained = tuple(row for row in rows if _retained(row, stage))
    return CandidateRecallDayStageMetrics(
        stage=stage,
        retained_count=len(retained),
        top_k_metrics=tuple(_top_k_metric(top_k, rows, retained) for top_k in CANDIDATE_RECALL_TOP_K),
        exit_untradable_rate=_rate(tuple(row.exit_untradable for row in retained)),
        severe_loss_rate=_rate(tuple(row.severe_loss for row in retained)),
        cumulative_latency_ms=cumulative_latency_ms,
    )


def _top_k_metric(
    top_k: int,
    population: tuple[CandidateRecallLedgerRow, ...],
    retained: tuple[CandidateRecallLedgerRow, ...],
) -> CandidateRecallTopKMetric:
    oracle = tuple(row for row in population if row.oracle_rank <= top_k)
    retained_codes = {row.code for row in retained}
    recalled = tuple(row for row in oracle if row.code in retained_codes)
    boards = tuple(
        CandidateRecallBoardRecall(
            board,
            sum(row.board is board for row in oracle),
            sum(row.board is board for row in recalled),
            _fraction(sum(row.board is board for row in recalled), sum(row.board is board for row in oracle)),
        )
        for board in _SUPPORTED_BOARDS
    )
    return CandidateRecallTopKMetric(
        top_k,
        len(oracle),
        len(recalled),
        _fraction(len(recalled), len(oracle)),
        boards,
    )


def _aggregate_stage(
    stage: CandidateRecallStage,
    days: tuple[CandidateRecallDayAttribution, ...],
) -> CandidateRecallAggregateStageMetrics:
    day_metrics = tuple(next(item for item in day.stages if item.stage == stage) for day in days)
    retained_rows = tuple(row for day in days for row in day.rows if _retained(row, stage))
    top_k_metrics = tuple(_aggregate_top_k(top_k, day_metrics) for top_k in CANDIDATE_RECALL_TOP_K)
    latencies = tuple(item.cumulative_latency_ms for item in day_metrics)
    return CandidateRecallAggregateStageMetrics(
        stage=stage,
        day_count=len(days),
        total_retained_count=len(retained_rows),
        mean_retained_count=len(retained_rows) / len(days),
        top_k_metrics=top_k_metrics,
        exit_untradable_rate=_rate(tuple(row.exit_untradable for row in retained_rows)),
        severe_loss_rate=_rate(tuple(row.severe_loss for row in retained_rows)),
        p50_cumulative_latency_ms=_percentile(latencies, 0.50),
        p95_cumulative_latency_ms=_percentile(latencies, 0.95),
    )


def _aggregate_top_k(
    top_k: int,
    stages: tuple[CandidateRecallDayStageMetrics, ...],
) -> CandidateRecallTopKMetric:
    metrics = tuple(next(item for item in stage.top_k_metrics if item.top_k == top_k) for stage in stages)
    oracle = sum(item.oracle_count for item in metrics)
    recalled = sum(item.recalled_count for item in metrics)
    boards = tuple(
        _aggregate_board(
            board, tuple(next(value for value in item.board_recalls if value.board is board) for item in metrics)
        )
        for board in _SUPPORTED_BOARDS
    )
    return CandidateRecallTopKMetric(top_k, oracle, recalled, _fraction(recalled, oracle), boards)


def _aggregate_board(
    board: Board,
    values: tuple[CandidateRecallBoardRecall, ...],
) -> CandidateRecallBoardRecall:
    oracle = sum(item.oracle_count for item in values)
    recalled = sum(item.recalled_count for item in values)
    return CandidateRecallBoardRecall(board, oracle, recalled, _fraction(recalled, oracle))


def _retained(row: CandidateRecallLedgerRow, stage: CandidateRecallStage) -> bool:
    boundary_index = len(CANDIDATE_RECALL_STAGES)
    if row.first_rejection_boundary != "selected":
        boundary_index = _BOUNDARY_INDEX[cast(CandidateRecallStage, row.first_rejection_boundary)]
    return boundary_index > _BOUNDARY_INDEX[stage]


def _fraction(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _rate(values: tuple[bool, ...]) -> float | None:
    return sum(values) / len(values) if values else None


def _percentile(values: tuple[float, ...], quantile: float) -> float:
    ordered = tuple(sorted(values))
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _validate_recall(oracle: int, recalled: int, recall: float | None, owner: str) -> None:
    if oracle < 0 or not 0 <= recalled <= oracle:
        raise ValueError(f"{owner} counts are invalid")
    expected = _fraction(recalled, oracle)
    if recall != expected:
        raise ValueError(f"{owner} rate does not match counts")


def _validate_rates(values: tuple[float | None, ...], owner: str) -> None:
    if any(value is not None and (not math.isfinite(value) or not 0.0 <= value <= 1.0) for value in values):
        raise ValueError(f"{owner} rates must be in [0, 1]")


__all__ = [
    "CANDIDATE_RECALL_STAGES",
    "CANDIDATE_RECALL_TOP_K",
    "CandidateRecallAggregateStageMetrics",
    "CandidateRecallBoardRecall",
    "CandidateRecallBoundary",
    "CandidateRecallDayAttribution",
    "CandidateRecallDayStageMetrics",
    "CandidateRecallDayTrace",
    "CandidateRecallDownstreamBoundary",
    "CandidateRecallDownstreamTrace",
    "CandidateRecallLedgerRow",
    "CandidateRecallReport",
    "CandidateRecallStage",
    "CandidateRecallStageLatency",
    "CandidateRecallState",
    "CandidateRecallTopKMetric",
    "CandidateRecallTraceMismatchError",
    "attribute_candidate_recall_day",
    "build_candidate_recall_report",
    "candidate_recall_insufficient_report",
]

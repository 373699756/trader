"""Immutable point-in-time research dataset identities and rows."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Literal
from zoneinfo import ZoneInfo

from trader.domain.market.feature_contracts import FeatureVector
from trader.domain.market.models import Board
from trader.domain.outcome.models import RecommendationOutcome
from trader.domain.research.h1_point_in_time import canonical_hash

PointInTimeDatasetState = Literal["historical_point_in_time_parity", "historical_data_insufficient"]
PointInTimePartitionName = Literal["training", "early_stopping", "calibration", "confirmation"]
PointInTimeRejectionBoundary = Literal[
    "eligible",
    "permanent_eligibility",
    "dynamic_hard_filter",
    "field_eligibility",
    "candidate_threshold",
    "board_limit",
]

POINT_IN_TIME_BENCHMARK_ID = "point_in_time_local_only_equal_weight"
POINT_IN_TIME_COST_BPS = (20, 50, 100)
POINT_IN_TIME_BOUNDARIES: tuple[PointInTimeRejectionBoundary, ...] = (
    "eligible",
    "permanent_eligibility",
    "dynamic_hard_filter",
    "field_eligibility",
    "candidate_threshold",
    "board_limit",
)
_HASH = re.compile(r"^[0-9a-f]{64}$")
_IDENTITY = re.compile(r"^[a-z0-9_]{1,96}$")
_SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class PointInTimeDateSplit:
    training_dates: tuple[date, ...]
    early_stopping_dates: tuple[date, ...]
    calibration_dates: tuple[date, ...]
    development_confirmation_embargo_dates: tuple[date, ...]
    confirmation_dates: tuple[date, ...]
    confirmation_holdout_embargo_dates: tuple[date, ...]
    terminal_holdout_dates: tuple[date, ...]
    terminal_holdout_opened: bool = False
    production_authority: bool = False
    schema_version: str = "point_in_time_date_split"
    date_set_hash: str = field(init=False)
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        groups = (
            tuple(self.training_dates),
            tuple(self.early_stopping_dates),
            tuple(self.calibration_dates),
            tuple(self.development_confirmation_embargo_dates),
            tuple(self.confirmation_dates),
            tuple(self.confirmation_holdout_embargo_dates),
            tuple(self.terminal_holdout_dates),
        )
        if any(not group for group in groups[:6]) or len(groups[3]) < 5 or len(groups[5]) < 5:
            raise ValueError("point-in-time date split requires development stages and five-day embargoes")
        if len(groups[-1]) < 200:
            raise ValueError("terminal holdout requires at least 200 dates")
        combined = tuple(item for group in groups for item in group)
        if combined != tuple(sorted(set(combined))):
            raise ValueError("point-in-time date split must be ordered and disjoint")
        if self.terminal_holdout_opened or self.production_authority:
            raise ValueError("point-in-time dataset split cannot open holdout or production")
        if self.schema_version != "point_in_time_date_split":
            raise ValueError("point-in-time date split schema is invalid")
        for name, group in zip(
            (
                "training_dates",
                "early_stopping_dates",
                "calibration_dates",
                "development_confirmation_embargo_dates",
                "confirmation_dates",
                "confirmation_holdout_embargo_dates",
                "terminal_holdout_dates",
            ),
            groups,
            strict=True,
        ):
            object.__setattr__(self, name, group)
        object.__setattr__(self, "date_set_hash", canonical_hash(combined))
        object.__setattr__(self, "content_hash", canonical_hash(self))

    @property
    def development_dates(self) -> tuple[date, ...]:
        return self.training_dates + self.early_stopping_dates + self.calibration_dates + self.confirmation_dates


@dataclass(frozen=True)
class PointInTimeEventFact:
    fact_id: str
    published_at: datetime
    effective_at: datetime
    anchor_at: datetime
    content_hash: str

    def __post_init__(self) -> None:
        if _IDENTITY.fullmatch(self.fact_id) is None or _HASH.fullmatch(self.content_hash) is None:
            raise ValueError("point-in-time event fact identity is invalid")
        for value in (self.published_at, self.effective_at, self.anchor_at):
            _require_shanghai(value, "point-in-time event fact")
        if self.published_at > self.anchor_at:
            raise ValueError("point-in-time event fact was not visible at the anchor")


@dataclass(frozen=True)
class PointInTimeIndustryFact:
    code: str
    industry: str
    classification: str
    effective_from: date
    effective_to: date | None
    queried_at: datetime
    source: str
    content_hash: str

    def __post_init__(self) -> None:
        if (
            len(self.code) != 6
            or not self.code.isdigit()
            or not self.industry.strip()
            or _IDENTITY.fullmatch(self.classification) is None
            or _IDENTITY.fullmatch(self.source) is None
            or _HASH.fullmatch(self.content_hash) is None
        ):
            raise ValueError("point-in-time industry fact identity is invalid")
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError("point-in-time industry interval is invalid")
        _require_shanghai(self.queried_at, "point-in-time industry fact")


@dataclass(frozen=True)
class PointInTimeSourceIdentity:
    daily_path_hash: str
    minute_path_hash: str
    security_fact_hash: str
    industry_fact_hash: str
    event_fact_hash: str

    def __post_init__(self) -> None:
        if any(_HASH.fullmatch(value) is None for value in self.hashes):
            raise ValueError("point-in-time source identity is invalid")

    @property
    def hashes(self) -> tuple[str, ...]:
        return (
            self.daily_path_hash,
            self.minute_path_hash,
            self.security_fact_hash,
            self.industry_fact_hash,
            self.event_fact_hash,
        )


@dataclass(frozen=True)
class PointInTimeCostOutcome:
    cost_bps: int
    outcome: RecommendationOutcome

    def __post_init__(self) -> None:
        if self.cost_bps not in POINT_IN_TIME_COST_BPS:
            raise ValueError("point-in-time outcome cost is invalid")
        if self.outcome.strategy.value != "tomorrow" or self.outcome.horizon != 1:
            raise ValueError("point-in-time dataset requires Tomorrow T+1 outcomes")
        if self.outcome.status == "complete":
            values = (
                self.outcome.gross_return_pct,
                self.outcome.benchmark_return_pct,
                self.outcome.net_excess_return_pct,
            )
            if any(value is None for value in values):
                raise ValueError("point-in-time complete outcome values are missing")
            gross, benchmark, net = (float(value) for value in values if value is not None)
            if not math.isclose(net, gross - benchmark - self.cost_bps / 100.0, abs_tol=1e-9):
                raise ValueError("point-in-time outcome cost was not applied exactly once")


@dataclass(frozen=True)
class PointInTimeDatasetRow:
    code: str
    trade_date: date
    anchor_at: datetime
    board: Board
    industry: str
    anchor_raw_price: float
    feature_vector: FeatureVector
    source_identity: PointInTimeSourceIdentity
    industry_fact: PointInTimeIndustryFact
    event_facts: tuple[PointInTimeEventFact, ...]
    first_rejection_boundary: PointInTimeRejectionBoundary
    rejection_reasons: tuple[str, ...]
    benchmark_eligible: bool
    candidate_eligible: bool
    candidate_score: float | None
    candidate_rank: int
    outcomes: tuple[PointInTimeCostOutcome, ...]
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _validate_row_identity(self)
        reasons = tuple(dict.fromkeys(self.rejection_reasons))
        if any(_IDENTITY.fullmatch(value) is None for value in reasons):
            raise ValueError("point-in-time rejection reasons are invalid")
        if (self.first_rejection_boundary == "eligible") == bool(reasons):
            raise ValueError("point-in-time rejection boundary and reasons are inconsistent")
        expected_benchmark = self.first_rejection_boundary in {"eligible", "candidate_threshold", "board_limit"}
        if self.benchmark_eligible is not expected_benchmark:
            raise ValueError("point-in-time benchmark eligibility is inconsistent")
        if self.candidate_eligible is not (self.first_rejection_boundary == "eligible"):
            raise ValueError("point-in-time candidate eligibility is inconsistent")
        if self.candidate_score is not None and not math.isfinite(self.candidate_score):
            raise ValueError("point-in-time candidate score is invalid")
        if self.candidate_rank < 0:
            raise ValueError("point-in-time candidate rank is invalid")
        facts = tuple(sorted(self.event_facts, key=lambda item: (item.fact_id, item.content_hash)))
        if len({(item.fact_id, item.content_hash) for item in facts}) != len(facts):
            raise ValueError("point-in-time event facts must be unique")
        outcomes = tuple(sorted(self.outcomes, key=lambda item: item.cost_bps))
        if tuple(item.cost_bps for item in outcomes) != POINT_IN_TIME_COST_BPS:
            raise ValueError("point-in-time row requires 20/50/100bp outcomes")
        if any(
            item.outcome.stock_code != self.code or item.outcome.recommend_date != self.trade_date.isoformat()
            for item in outcomes
        ):
            raise ValueError("point-in-time outcome identity does not match the row")
        object.__setattr__(self, "event_facts", facts)
        object.__setattr__(self, "rejection_reasons", reasons)
        object.__setattr__(self, "outcomes", outcomes)
        object.__setattr__(self, "content_hash", canonical_hash(self))

    @property
    def label_complete(self) -> bool:
        return all(item.outcome.status == "complete" for item in self.outcomes)


def _validate_row_identity(row: PointInTimeDatasetRow) -> None:
    if len(row.code) != 6 or not row.code.isdigit() or row.board is Board.UNSUPPORTED or not row.industry.strip():
        raise ValueError("point-in-time row identity is invalid")
    _require_shanghai(row.anchor_at, "point-in-time row")
    local = row.anchor_at.astimezone(_SHANGHAI)
    if local.date() != row.trade_date or local.timetz().replace(tzinfo=None) != time(14, 50):
        raise ValueError("point-in-time row requires the Tomorrow 14:50 anchor")
    if not math.isfinite(row.anchor_raw_price) or row.anchor_raw_price <= 0.0:
        raise ValueError("point-in-time row raw anchor price is invalid")
    if any(item.anchor_at != row.anchor_at for item in row.event_facts):
        raise ValueError("point-in-time event fact anchor does not match the row")
    industry = row.industry_fact
    if (
        industry.code != row.code
        or industry.industry != row.industry
        or industry.content_hash != row.source_identity.industry_fact_hash
        or row.trade_date < industry.effective_from
        or (industry.effective_to is not None and row.trade_date >= industry.effective_to)
    ):
        raise ValueError("point-in-time industry fact does not apply to the row")


@dataclass(frozen=True)
class PointInTimeBoundaryCount:
    boundary: PointInTimeRejectionBoundary
    count: int

    def __post_init__(self) -> None:
        if self.count < 0:
            raise ValueError("point-in-time boundary count is invalid")


@dataclass(frozen=True)
class PointInTimeCoverage:
    total_rows: int
    benchmark_eligible_rows: int
    candidate_eligible_rows: int
    label_complete_rows: int
    boundary_counts: tuple[PointInTimeBoundaryCount, ...]

    def __post_init__(self) -> None:
        counts = (
            self.total_rows,
            self.benchmark_eligible_rows,
            self.candidate_eligible_rows,
            self.label_complete_rows,
        )
        if any(value < 0 or value > self.total_rows for value in counts):
            raise ValueError("point-in-time coverage counts are invalid")
        boundaries = tuple(self.boundary_counts)
        if tuple(item.boundary for item in boundaries) != POINT_IN_TIME_BOUNDARIES:
            raise ValueError("point-in-time boundary counts must use the fixed order")
        if sum(item.count for item in boundaries) != self.total_rows:
            raise ValueError("point-in-time boundary counts must cover every row")
        object.__setattr__(self, "boundary_counts", boundaries)


@dataclass(frozen=True)
class PointInTimeBoardPopulation:
    board: Board
    population_version: str
    eligible_rows: int
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.board is Board.UNSUPPORTED or not self.population_version or self.eligible_rows < 1:
            raise ValueError("point-in-time board population identity is invalid")
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class PointInTimeDayDataset:
    trade_date: date
    anchor_at: datetime
    rows: tuple[PointInTimeDatasetRow, ...]
    coverage: PointInTimeCoverage
    board_populations: tuple[PointInTimeBoardPopulation, ...]
    benchmark_identity: str = POINT_IN_TIME_BENCHMARK_ID
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        rows = tuple(sorted(self.rows, key=lambda item: item.code))
        if not rows or len({item.code for item in rows}) != len(rows):
            raise ValueError("point-in-time day requires unique rows")
        if any(item.trade_date != self.trade_date or item.anchor_at != self.anchor_at for item in rows):
            raise ValueError("point-in-time day row coordinates are inconsistent")
        if self.benchmark_identity != POINT_IN_TIME_BENCHMARK_ID:
            raise ValueError("point-in-time benchmark identity is invalid")
        actual_boundaries = {boundary: 0 for boundary in POINT_IN_TIME_BOUNDARIES}
        for row in rows:
            actual_boundaries[row.first_rejection_boundary] += 1
        if (
            self.coverage.total_rows != len(rows)
            or self.coverage.benchmark_eligible_rows != sum(item.benchmark_eligible for item in rows)
            or self.coverage.candidate_eligible_rows != sum(item.candidate_eligible for item in rows)
            or self.coverage.label_complete_rows != sum(item.label_complete for item in rows)
            or tuple(item.count for item in self.coverage.boundary_counts)
            != tuple(actual_boundaries[boundary] for boundary in POINT_IN_TIME_BOUNDARIES)
        ):
            raise ValueError("point-in-time day coverage is inconsistent")
        populations = tuple(sorted(self.board_populations, key=lambda item: item.board.value))
        if not populations or len({item.board for item in populations}) != len(populations):
            raise ValueError("point-in-time day board populations are invalid")
        expected_boards = {item.board for item in rows if item.benchmark_eligible}
        if {item.board for item in populations} != expected_boards:
            raise ValueError("point-in-time day board population coverage is incomplete")
        benchmark_counts = {
            board: sum(item.benchmark_eligible and item.board is board for item in rows) for board in Board
        }
        if any(benchmark_counts[item.board] != item.eligible_rows for item in populations):
            raise ValueError("point-in-time day board population counts are inconsistent")
        object.__setattr__(self, "rows", rows)
        object.__setattr__(self, "board_populations", populations)
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class PointInTimePartitionManifest:
    name: PointInTimePartitionName
    dates: tuple[date, ...]
    day_hashes: tuple[str, ...]
    row_count: int
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        dates = tuple(self.dates)
        if self.name not in ("training", "early_stopping", "calibration", "confirmation"):
            raise ValueError("point-in-time partition name is invalid")
        if not dates or dates != tuple(sorted(set(dates))) or len(dates) != len(self.day_hashes):
            raise ValueError("point-in-time partition dates are invalid")
        if any(_HASH.fullmatch(value) is None for value in self.day_hashes) or self.row_count < 0:
            raise ValueError("point-in-time partition identity is invalid")
        object.__setattr__(self, "dates", dates)
        object.__setattr__(self, "day_hashes", tuple(self.day_hashes))
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class PointInTimeDatasetManifest:
    qualification_hash: str
    daily_archive_manifest_hash: str
    feature_manifest_hash: str
    calendar_hash: str
    security_master_hash: str
    selection_policy_hash: str
    date_split: PointInTimeDateSplit
    date_split_hash: str
    partitions: tuple[PointInTimePartitionManifest, ...]
    total_rows: int
    terminal_holdout_rows: int = 0
    terminal_holdout_opened: bool = False
    production_authority: bool = False
    schema_version: str = "point_in_time_dataset_manifest"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        hashes = (
            self.qualification_hash,
            self.daily_archive_manifest_hash,
            self.feature_manifest_hash,
            self.calendar_hash,
            self.security_master_hash,
            self.selection_policy_hash,
            self.date_split_hash,
        )
        partitions = tuple(self.partitions)
        if any(_HASH.fullmatch(value) is None for value in hashes):
            raise ValueError("point-in-time dataset parent identity is invalid")
        if tuple(item.name for item in partitions) != ("training", "early_stopping", "calibration", "confirmation"):
            raise ValueError("point-in-time dataset requires fixed development partitions")
        expected_dates = (
            self.date_split.training_dates,
            self.date_split.early_stopping_dates,
            self.date_split.calibration_dates,
            self.date_split.confirmation_dates,
        )
        if (
            self.date_split_hash != self.date_split.content_hash
            or tuple(item.dates for item in partitions) != expected_dates
        ):
            raise ValueError("point-in-time dataset date split binding is invalid")
        if self.total_rows != sum(item.row_count for item in partitions) or self.total_rows < 1:
            raise ValueError("point-in-time dataset row count is invalid")
        if self.terminal_holdout_rows != 0 or self.terminal_holdout_opened or self.production_authority:
            raise ValueError("point-in-time dataset cannot read holdout or authorize production")
        if self.schema_version != "point_in_time_dataset_manifest":
            raise ValueError("point-in-time dataset manifest schema is invalid")
        object.__setattr__(self, "partitions", partitions)
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class PointInTimeDatasetReport:
    qualification_hash: str
    state: PointInTimeDatasetState
    days: tuple[PointInTimeDayDataset, ...]
    manifest: PointInTimeDatasetManifest | None
    failure_reasons: tuple[str, ...]
    terminal_holdout_opened: bool = False
    production_authority: bool = False
    schema_version: str = "point_in_time_dataset_report"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        days = tuple(sorted(self.days, key=lambda item: item.trade_date))
        reasons = tuple(sorted(set(self.failure_reasons)))
        if _HASH.fullmatch(self.qualification_hash) is None or any(
            _IDENTITY.fullmatch(value) is None for value in reasons
        ):
            raise ValueError("point-in-time dataset report identity is invalid")
        if self.state == "historical_point_in_time_parity":
            if not days or self.manifest is None or reasons:
                raise ValueError("point-in-time parity report is incomplete")
            if self.manifest.qualification_hash != self.qualification_hash:
                raise ValueError("point-in-time dataset manifest parent does not match")
            by_date = {item.trade_date: item for item in days}
            expected_dates = tuple(
                trade_date for partition in self.manifest.partitions for trade_date in partition.dates
            )
            if (
                tuple(by_date) != tuple(sorted(expected_dates))
                or any(
                    tuple(by_date[trade_date].content_hash for trade_date in partition.dates) != partition.day_hashes
                    or sum(len(by_date[trade_date].rows) for trade_date in partition.dates) != partition.row_count
                    for partition in self.manifest.partitions
                )
                or any(
                    row.feature_vector.manifest_hash != self.manifest.feature_manifest_hash
                    for day in days
                    for row in day.rows
                )
            ):
                raise ValueError("point-in-time dataset report does not match its partitions")
        elif self.state == "historical_data_insufficient":
            if days or self.manifest is not None or not reasons:
                raise ValueError("insufficient point-in-time report must fail closed")
        else:
            raise ValueError("point-in-time dataset state is invalid")
        if self.terminal_holdout_opened or self.production_authority:
            raise ValueError("point-in-time dataset report cannot open holdout or production")
        if self.schema_version != "point_in_time_dataset_report":
            raise ValueError("point-in-time dataset report schema is invalid")
        object.__setattr__(self, "days", days)
        object.__setattr__(self, "failure_reasons", reasons)
        object.__setattr__(self, "content_hash", canonical_hash(self))


def _require_shanghai(value: datetime, owner: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None or getattr(value.tzinfo, "key", None) != "Asia/Shanghai":
        raise ValueError(f"{owner} time must use Asia/Shanghai")


__all__ = [
    "POINT_IN_TIME_BENCHMARK_ID",
    "POINT_IN_TIME_BOUNDARIES",
    "POINT_IN_TIME_COST_BPS",
    "PointInTimeBoundaryCount",
    "PointInTimeBoardPopulation",
    "PointInTimeCostOutcome",
    "PointInTimeCoverage",
    "PointInTimeDatasetManifest",
    "PointInTimeDatasetReport",
    "PointInTimeDatasetRow",
    "PointInTimeDatasetState",
    "PointInTimeDateSplit",
    "PointInTimeDayDataset",
    "PointInTimeEventFact",
    "PointInTimeIndustryFact",
    "PointInTimePartitionManifest",
    "PointInTimePartitionName",
    "PointInTimeRejectionBoundary",
    "PointInTimeSourceIdentity",
]

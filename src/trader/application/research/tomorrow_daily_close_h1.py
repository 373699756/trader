"""Two-stage H1 feature and matured-label adapter for Tomorrow C3."""

from __future__ import annotations

import dataclasses
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from trader.application.research.replay_models import canonical_hash
from trader.application.research.tomorrow_daily_close_training import DailyCloseBoard, DailyCloseSourceSample
from trader.domain.research.h1_point_in_time import H1PointInTimeRecord

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FEATURE_NAMES = (
    "qfq_return_1d",
    "qfq_return_3d",
    "qfq_return_5d",
    "qfq_residual_momentum_20d_skip5",
    "qfq_residual_momentum_40d_skip5",
    "qfq_residual_momentum_60d_skip5",
)
_FEATURE_UNITS = ("ratio",) * 6
_COSTS = (0.002, 0.005, 0.01)


@dataclass(frozen=True)
class H1DailyCloseObservation:
    record: H1PointInTimeRecord
    board: DailyCloseBoard
    hard_filter_passed: bool
    hard_filter_evidence_complete: bool
    filter_evidence_hash: str | None
    schema_version: str = "tomorrow_h1_daily_close_observation_v1"

    def __post_init__(self) -> None:
        if self.record.strategy != "tomorrow" or self.board not in {"main", "chinext", "star"}:
            raise ValueError("Tomorrow H1 daily-close observation identity is invalid")
        if self.hard_filter_evidence_complete:
            if self.filter_evidence_hash is None or _SHA256.fullmatch(self.filter_evidence_hash) is None:
                raise ValueError("complete Tomorrow H1 filter evidence requires a SHA-256")
        elif self.filter_evidence_hash is not None:
            raise ValueError("incomplete Tomorrow H1 filter evidence cannot claim a hash")
        if self.schema_version != "tomorrow_h1_daily_close_observation_v1":
            raise ValueError("Tomorrow H1 daily-close observation schema is invalid")


@dataclass(frozen=True)
class H1DailyCloseFeatureRow:
    trade_date: date
    code: str
    board: DailyCloseBoard
    feature_values: tuple[float, float, float, float, float, float]
    hard_filter_passed: bool
    hard_filter_evidence_complete: bool
    filter_evidence_hash: str | None
    source_record_hash: str
    schema_version: str = "tomorrow_h1_daily_close_feature_row_v1"
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        if len(self.code) != 6 or not self.code.isdigit() or self.board not in {"main", "chinext", "star"}:
            raise ValueError("Tomorrow H1 daily-close feature identity is invalid")
        if len(self.feature_values) != 6 or not all(math.isfinite(value) for value in self.feature_values):
            raise ValueError("Tomorrow H1 daily-close feature vector is invalid")
        if _SHA256.fullmatch(self.source_record_hash) is None:
            raise ValueError("Tomorrow H1 daily-close source identity is invalid")
        if self.hard_filter_evidence_complete != (self.filter_evidence_hash is not None) or (
            self.filter_evidence_hash is not None and _SHA256.fullmatch(self.filter_evidence_hash) is None
        ):
            raise ValueError("Tomorrow H1 daily-close filter evidence is inconsistent")
        if self.schema_version != "tomorrow_h1_daily_close_feature_row_v1":
            raise ValueError("Tomorrow H1 daily-close feature row schema is invalid")
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class H1DailyCloseFeatureBatch:
    rows: tuple[H1DailyCloseFeatureRow, ...]
    source_archive_hash: str
    feature_names: tuple[str, ...] = _FEATURE_NAMES
    feature_units: tuple[str, ...] = _FEATURE_UNITS
    schema_version: str = "tomorrow_h1_daily_close_feature_batch_v1"
    production_authority: bool = False
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        ordered = tuple(sorted(self.rows, key=lambda item: (item.trade_date, item.code)))
        identities = tuple((item.trade_date, item.code) for item in ordered)
        if not ordered or len(identities) != len(set(identities)):
            raise ValueError("Tomorrow H1 feature batch identities are invalid")
        if _SHA256.fullmatch(self.source_archive_hash) is None:
            raise ValueError("Tomorrow H1 feature batch source identity is invalid")
        if self.feature_names != _FEATURE_NAMES or self.feature_units != _FEATURE_UNITS:
            raise ValueError("Tomorrow H1 feature batch contract is invalid")
        if self.schema_version != "tomorrow_h1_daily_close_feature_batch_v1" or self.production_authority:
            raise ValueError("Tomorrow H1 feature batch cannot authorize production")
        object.__setattr__(self, "rows", ordered)
        object.__setattr__(self, "content_hash", canonical_hash(self))


def build_h1_daily_close_features(
    observations: tuple[H1DailyCloseObservation, ...],
) -> H1DailyCloseFeatureBatch:
    ordered = tuple(sorted(observations, key=lambda item: (item.record.trade_date, item.record.code)))
    identities = tuple((item.record.trade_date, item.record.code) for item in ordered)
    if not ordered or len(identities) != len(set(identities)):
        raise ValueError("Tomorrow H1 observations must be unique by date and code")
    histories: dict[str, list[H1DailyCloseObservation]] = defaultdict(list)
    raw_by_date: dict[
        date,
        list[
            tuple[
                H1DailyCloseObservation,
                tuple[float, float, float],
                tuple[float, float, float],
                float,
            ]
        ],
    ] = defaultdict(list)
    for observation in ordered:
        history = histories[observation.record.code]
        history.append(observation)
        if len(history) < 66:
            continue
        closes = tuple(item.record.daily_bar.close for item in history)
        amounts = tuple(item.record.daily_bar.amount for item in history[-20:])
        base = (_return(closes, 1), _return(closes, 3), _return(closes, 5))
        momentum = (
            _skip_five_return(closes, 20),
            _skip_five_return(closes, 40),
            _skip_five_return(closes, 60),
        )
        log_amount = math.log(max(math.fsum(amounts) / len(amounts), 1e-12))
        raw_by_date[observation.record.trade_date].append((observation, base, momentum, log_amount))
    rows: list[H1DailyCloseFeatureRow] = []
    for day in sorted(raw_by_date):
        day_rows = raw_by_date[day]
        residuals = _cross_section_residuals(day_rows)
        for (observation, base, _, _), residual in zip(day_rows, residuals, strict=True):
            rows.append(
                H1DailyCloseFeatureRow(
                    day,
                    observation.record.code,
                    observation.board,
                    (*base, *residual),
                    observation.hard_filter_passed,
                    observation.hard_filter_evidence_complete,
                    observation.filter_evidence_hash,
                    observation.record.content_hash,
                )
            )
    return H1DailyCloseFeatureBatch(
        tuple(rows),
        canonical_hash(tuple(item.record.content_hash for item in ordered)),
    )


def attach_matured_daily_close_labels(
    feature_batch: H1DailyCloseFeatureBatch,
    observations: tuple[H1DailyCloseObservation, ...],
) -> tuple[DailyCloseSourceSample, ...]:
    by_code: dict[str, list[H1DailyCloseObservation]] = defaultdict(list)
    for observation in sorted(observations, key=lambda item: (item.record.code, item.record.trade_date)):
        by_code[observation.record.code].append(observation)
    next_by_identity: dict[tuple[date, str], H1DailyCloseObservation] = {}
    current_by_identity: dict[tuple[date, str], H1DailyCloseObservation] = {}
    for code, values in by_code.items():
        for item in values:
            current_by_identity[(item.record.trade_date, code)] = item
        for current, following in zip(values, values[1:], strict=False):
            next_by_identity[(current.record.trade_date, code)] = following
    gross_by_date: dict[date, list[float]] = defaultdict(list)
    gross_by_identity: dict[tuple[date, str], float] = {}
    for row in feature_batch.rows:
        following_for_return = next_by_identity.get((row.trade_date, row.code))
        current = current_by_identity[(row.trade_date, row.code)]
        if following_for_return is None or following_for_return.record.daily_bar.volume <= 0.0:
            continue
        gross_return = following_for_return.record.daily_bar.close / current.record.daily_bar.close - 1.0
        gross_by_identity[(row.trade_date, row.code)] = gross_return
        if row.hard_filter_evidence_complete and row.hard_filter_passed:
            gross_by_date[row.trade_date].append(gross_return)
    samples: list[DailyCloseSourceSample] = []
    for row in feature_batch.rows:
        identity = (row.trade_date, row.code)
        following_for_label = next_by_identity.get(identity)
        gross_for_label = gross_by_identity.get(identity)
        benchmark_population = gross_by_date.get(row.trade_date)
        if following_for_label is None or gross_for_label is None or not benchmark_population:
            continue
        benchmark = math.fsum(benchmark_population) / len(benchmark_population)
        net_excess = (
            gross_for_label - benchmark - _COSTS[0],
            gross_for_label - benchmark - _COSTS[1],
            gross_for_label - benchmark - _COSTS[2],
        )
        samples.append(
            DailyCloseSourceSample(
                trade_date=row.trade_date,
                label_maturity_date=following_for_label.record.trade_date,
                code=row.code,
                board=row.board,
                feature_values=row.feature_values,
                net_excess_returns=net_excess,
                hard_filter_passed=row.hard_filter_passed,
                hard_filter_evidence_complete=row.hard_filter_evidence_complete,
                filter_evidence_hash=row.filter_evidence_hash,
                source_row_hash=canonical_hash(
                    {
                        "feature_hash": row.content_hash,
                        "label_record_hash": following_for_label.record.content_hash,
                        "benchmark": benchmark,
                    }
                ),
            )
        )
    return tuple(samples)


def _return(closes: tuple[float, ...], sessions: int) -> float:
    return closes[-1] / closes[-sessions - 1] - 1.0


def _skip_five_return(closes: tuple[float, ...], window: int) -> float:
    return closes[-6] / closes[-window - 6] - 1.0


def _cross_section_residuals(
    rows: list[
        tuple[
            H1DailyCloseObservation,
            tuple[float, float, float],
            tuple[float, float, float],
            float,
        ]
    ],
) -> tuple[tuple[float, float, float], ...]:
    result = [[0.0, 0.0, 0.0] for _ in rows]
    boards = sorted({item[0].board for item in rows})
    for feature_index in range(3):
        market_mean = math.fsum(item[2][feature_index] for item in rows) / len(rows)
        for board in boards:
            indexes = tuple(index for index, item in enumerate(rows) if item[0].board == board)
            values = tuple(rows[index][2][feature_index] - market_mean for index in indexes)
            exposures = tuple(rows[index][3] for index in indexes)
            value_mean = math.fsum(values) / len(values)
            exposure_mean = math.fsum(exposures) / len(exposures)
            denominator = math.fsum((value - exposure_mean) ** 2 for value in exposures)
            beta = (
                math.fsum(
                    (exposure - exposure_mean) * (value - value_mean)
                    for exposure, value in zip(exposures, values, strict=True)
                )
                / denominator
                if denominator > 1e-18
                else 0.0
            )
            for index, value, exposure in zip(indexes, values, exposures, strict=True):
                result[index][feature_index] = value - value_mean - beta * (exposure - exposure_mean)
    return tuple((values[0], values[1], values[2]) for values in result)


__all__ = [
    "H1DailyCloseFeatureBatch",
    "H1DailyCloseFeatureRow",
    "H1DailyCloseObservation",
    "attach_matured_daily_close_labels",
    "build_h1_daily_close_features",
]

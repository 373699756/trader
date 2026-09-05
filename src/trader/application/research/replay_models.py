"""Immutable Score-R3 baseline replay and report values."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import date
from typing import Literal

from trader.domain.research.specification import SCORE_P0_V1_SPEC, get_score_research_spec

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COST_RATES = (0.002, 0.005, 0.01)


@dataclass(frozen=True)
class BaselineReplaySelection:
    code: str
    production_rank: int | None
    oracle_rank: int | None

    def __post_init__(self) -> None:
        _code(self.code)
        if any(rank is not None and not 1 <= rank <= 6 for rank in (self.production_rank, self.oracle_rank)):
            raise ValueError("baseline selection ranks must identify Top6 items")


@dataclass(frozen=True)
class BaselineDayMetrics:
    trade_date: date
    day_hash: str
    input_hash: str
    selected_codes: tuple[str, ...]
    oracle_codes: tuple[str, ...]
    selection_status: Literal["selected", "no_decision"]
    evaluated_count: int
    oracle_selected_count: int
    recalled_oracle_count: int
    net_excess_returns: tuple[float, float, float]
    mean_mae_atr20: float | None
    severe_drawdown_rate: float | None
    candidate_recall: float | None
    field_coverage: float
    maximum_board_fraction: float
    maximum_industry_fraction: float
    rank_ic: float | None
    score_bucket_net_excess_20bp: tuple[float | None, float | None, float | None, float | None, float | None]
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        _hash(self.day_hash, "baseline day")
        _hash(self.input_hash, "baseline input")
        if len(set(self.selected_codes)) != len(self.selected_codes) or len(set(self.oracle_codes)) != len(
            self.oracle_codes
        ):
            raise ValueError("baseline production and oracle codes must be unique")
        for code in (*self.selected_codes, *self.oracle_codes):
            _code(code)
        if self.selection_status != ("selected" if self.selected_codes else "no_decision"):
            raise ValueError("baseline selection status must match the production selection")
        if (
            self.evaluated_count < max(len(self.selected_codes), len(self.oracle_codes))
            or self.oracle_selected_count != len(self.oracle_codes)
            or not 0 <= self.recalled_oracle_count <= self.oracle_selected_count <= self.evaluated_count
        ):
            raise ValueError("baseline report candidate counts are inconsistent")
        expected_recall = (
            self.recalled_oracle_count / self.oracle_selected_count if self.oracle_selected_count else None
        )
        if self.candidate_recall != expected_recall:
            raise ValueError("baseline candidate recall must match its micro-average counts")
        if bool(self.selected_codes) != (self.mean_mae_atr20 is not None and self.severe_drawdown_rate is not None):
            raise ValueError("baseline selected metrics must match the production selection")
        _finite(self.net_excess_returns)
        _optional_finite((self.mean_mae_atr20, self.severe_drawdown_rate, self.candidate_recall, self.rank_ic))
        _optional_finite(self.score_bucket_net_excess_20bp)
        for value in (
            self.severe_drawdown_rate,
            self.candidate_recall,
            self.field_coverage,
            self.maximum_board_fraction,
            self.maximum_industry_fraction,
        ):
            if value is not None and not 0.0 <= value <= 1.0:
                raise ValueError("baseline rate metrics must be in [0, 1]")
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class BaselineAggregateMetrics:
    net_excess_returns: tuple[float, float, float]
    mean_mae_atr20: float | None
    severe_drawdown_rate: float | None
    candidate_recall: float | None
    field_coverage: float
    mean_maximum_board_fraction: float
    mean_maximum_industry_fraction: float
    mean_rank_ic: float | None
    score_bucket_net_excess_20bp: tuple[float | None, float | None, float | None, float | None, float | None]

    def __post_init__(self) -> None:
        _finite(self.net_excess_returns)
        _optional_finite((self.mean_mae_atr20, self.severe_drawdown_rate, self.candidate_recall, self.mean_rank_ic))
        _optional_finite(self.score_bucket_net_excess_20bp)
        for value in (
            self.severe_drawdown_rate,
            self.candidate_recall,
            self.field_coverage,
            self.mean_maximum_board_fraction,
            self.mean_maximum_industry_fraction,
        ):
            if value is not None and not 0.0 <= value <= 1.0:
                raise ValueError("aggregate baseline rate metrics must be in [0, 1]")


BaselineReportStatus = Literal["replayed", "exploratory"]


@dataclass(frozen=True)
class ScoreR3BaselineReport:
    status: BaselineReportStatus
    extraction_hash: str
    extraction_status: Literal["extracted", "exploratory"]
    days: tuple[BaselineDayMetrics, ...]
    aggregate: BaselineAggregateMetrics
    research_identity: str = dataclasses.field(
        default=SCORE_P0_V1_SPEC.research_identity,
        metadata={"exclude_from_v1_hash": True},
    )
    research_spec_hash: str = dataclasses.field(
        default=SCORE_P0_V1_SPEC.content_hash,
        metadata={"exclude_from_v1_hash": True},
    )
    schema_version: str = "score_r3_baseline_report"
    replay_version: str = "production_local_baseline"
    cost_rates: tuple[float, float, float] = _COST_RATES
    report_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        _hash(self.extraction_hash, "Score-R3 extraction")
        spec = get_score_research_spec(self.research_identity)
        if self.research_spec_hash != spec.content_hash:
            raise ValueError("Score-R3 report research spec hash is invalid")
        expected_schema = (
            "score_r3_candidate_report" if self.research_identity == "score_p0_v2" else "score_r3_baseline_report"
        )
        if self.schema_version != expected_schema or self.replay_version != "production_local_baseline":
            raise ValueError("Score-R3 report identity is invalid")
        if self.cost_rates != _COST_RATES:
            raise ValueError("Score-R3 costs must remain 20bp, 50bp, and 100bp")
        days = tuple(sorted(self.days, key=lambda item: item.trade_date))
        if len(days) > 40 or len({item.trade_date for item in days}) != len(days):
            raise ValueError("Score-R3 report accepts at most 40 unique historical days")
        if any(item.trade_date not in spec.allowed_historical_dates for item in days):
            raise ValueError("Score-R3 report contains dates outside its research spec")
        expected_extraction_status = "extracted" if len(days) == 40 else "exploratory"
        if self.extraction_status != expected_extraction_status:
            raise ValueError("Score-R3 extraction status must match its valid-day evidence")
        if self.status != ("replayed" if self.extraction_status == "extracted" else "exploratory"):
            raise ValueError("Score-R3 report status must match its valid-day evidence")
        object.__setattr__(self, "days", days)
        object.__setattr__(self, "report_hash", canonical_hash(self))

    @property
    def day_count(self) -> int:
        return len(self.days)


def canonical_hash(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def canonical_json(value: object) -> str:
    return json.dumps(canonical_value(value), ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))


def canonical_value(value: object) -> object:
    if dataclasses.is_dataclass(value):
        legacy_identity = getattr(value, "research_identity", None) == SCORE_P0_V1_SPEC.research_identity
        return {
            field.name: canonical_value(getattr(value, field.name))
            for field in dataclasses.fields(value)
            if field.init and not (legacy_identity and field.metadata.get("exclude_from_v1_hash", False))
        }
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (tuple, list)):
        return [canonical_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): canonical_value(item) for key, item in value.items()}
    return value


def _code(code: str) -> None:
    if len(code) != 6 or not code.isdigit():
        raise ValueError("research stock code must contain exactly six digits")


def _hash(value: str, label: str) -> None:
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} identity must be SHA-256")


def _finite(values: tuple[float, ...]) -> None:
    if any(not math.isfinite(value) for value in values):
        raise ValueError("baseline numeric metrics must be finite")


def _optional_finite(values: tuple[float | None, ...]) -> None:
    if any(value is not None and not math.isfinite(value) for value in values):
        raise ValueError("optional baseline metrics must be finite when present")


__all__ = [
    "BaselineAggregateMetrics",
    "BaselineDayMetrics",
    "BaselineReportStatus",
    "BaselineReplaySelection",
    "ScoreR3BaselineReport",
    "canonical_hash",
    "canonical_json",
    "canonical_value",
]

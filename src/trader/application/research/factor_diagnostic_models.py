"""Typed values for immutable native score-factor diagnostics."""

from __future__ import annotations

import dataclasses
import math
import re
from dataclasses import dataclass
from datetime import date
from typing import Literal

from trader.application.research.replay_models import canonical_hash
from trader.domain.research.specification import get_score_research_spec

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COST_RATES = (0.002, 0.005, 0.01)
_DECAY_LAGS = (1, 3, 5)
QuintileValues = tuple[float | None, float | None, float | None, float | None, float | None]
DiagnosticStatus = Literal["evaluated", "exploratory"]
StratumDimension = Literal["board", "industry", "market_cap", "liquidity"]


@dataclass(frozen=True)
class FactorDiagnosticDimensionRecord:
    trade_date: date
    day_hash: str
    input_hash: str
    code: str
    market_cap: float | None
    liquidity: float | None

    def __post_init__(self) -> None:
        _hash(self.day_hash, "factor dimension day")
        _hash(self.input_hash, "factor dimension input")
        _code(self.code)
        _optional_finite((self.market_cap, self.liquidity))
        if any(value is not None and value < 0.0 for value in (self.market_cap, self.liquidity)):
            raise ValueError("factor dimensions must be non-negative")


@dataclass(frozen=True)
class FactorDiagnosticDimensions:
    extraction_hash: str
    records: tuple[FactorDiagnosticDimensionRecord, ...]
    schema_version: str = "score_factor_dimensions_v1"
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        _hash(self.extraction_hash, "factor dimension extraction")
        if self.schema_version != "score_factor_dimensions_v1":
            raise ValueError("factor dimension schema is invalid")
        records = tuple(sorted(self.records, key=lambda item: (item.trade_date, item.code)))
        if len({(item.trade_date, item.code) for item in records}) != len(records):
            raise ValueError("factor dimension records must be unique")
        object.__setattr__(self, "records", records)
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class FactorCostQuintiles:
    cost_rate: float
    quintile_net_excess: QuintileValues
    adjacent_monotonic_fraction: float | None
    top_minus_bottom: float | None

    def __post_init__(self) -> None:
        if self.cost_rate not in _COST_RATES:
            raise ValueError("factor diagnostic cost rate is invalid")
        _optional_finite(self.quintile_net_excess)
        _optional_finite((self.adjacent_monotonic_fraction, self.top_minus_bottom))
        _optional_rate(self.adjacent_monotonic_fraction)


@dataclass(frozen=True)
class FactorDailyDiagnostic:
    trade_date: date
    day_hash: str
    input_hash: str
    total_count: int
    observed_count: int
    coverage: float
    missing_rate: float
    ic: float | None
    rank_ic: float | None
    quintile_counts: tuple[int, int, int, int, int]
    cost_quintiles: tuple[FactorCostQuintiles, ...]
    severe_rate_by_quintile: QuintileValues
    mean_mae_atr20_by_quintile: QuintileValues
    top_quintile_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        _hash(self.day_hash, "factor diagnostic day")
        _hash(self.input_hash, "factor diagnostic input")
        _counts(self.total_count, self.observed_count, self.coverage, self.missing_rate)
        if len(self.quintile_counts) != 5 or sum(self.quintile_counts) != self.observed_count:
            raise ValueError("factor diagnostic quintile counts are inconsistent")
        if tuple(item.cost_rate for item in self.cost_quintiles) != _COST_RATES:
            raise ValueError("factor daily costs must remain 20bp, 50bp, and 100bp")
        _optional_finite((self.ic, self.rank_ic))
        _optional_finite((*self.severe_rate_by_quintile, *self.mean_mae_atr20_by_quintile))
        for value in self.severe_rate_by_quintile:
            _optional_rate(value)
        if len(self.top_quintile_codes) != len(set(self.top_quintile_codes)):
            raise ValueError("factor top-quintile codes must be unique")
        for code in self.top_quintile_codes:
            _code(code)
        object.__setattr__(self, "top_quintile_codes", tuple(sorted(self.top_quintile_codes)))


@dataclass(frozen=True)
class FactorLagDiagnostic:
    lag: int
    decay_rank_ic: float | None
    top_quintile_turnover: float | None
    valid_decay_pairs: int
    valid_turnover_pairs: int

    def __post_init__(self) -> None:
        if self.lag not in _DECAY_LAGS or min(self.valid_decay_pairs, self.valid_turnover_pairs) < 0:
            raise ValueError("factor lag diagnostic identity is invalid")
        _optional_finite((self.decay_rank_ic, self.top_quintile_turnover))
        _optional_rate(self.top_quintile_turnover)


@dataclass(frozen=True)
class FactorStratumDiagnostic:
    dimension: StratumDimension
    label: str
    total_count: int
    observed_count: int
    coverage: float
    missing_rate: float
    mean_ic: float | None
    mean_rank_ic: float | None
    mean_net_excess_20bp: float | None
    severe_loss_rate: float | None
    mean_mae_atr20: float | None

    def __post_init__(self) -> None:
        if self.dimension not in {"board", "industry", "market_cap", "liquidity"} or not self.label:
            raise ValueError("factor stratum identity is invalid")
        _counts(self.total_count, self.observed_count, self.coverage, self.missing_rate)
        _optional_finite(
            (self.mean_ic, self.mean_rank_ic, self.mean_net_excess_20bp, self.severe_loss_rate, self.mean_mae_atr20)
        )
        _optional_rate(self.severe_loss_rate)


@dataclass(frozen=True)
class FactorAggregateDiagnostic:
    factor_name: str
    total_count: int
    observed_count: int
    coverage: float
    missing_rate: float
    mean_ic: float | None
    mean_rank_ic: float | None
    icir: float | None
    cost_quintiles: tuple[FactorCostQuintiles, ...]
    severe_rate_by_quintile: QuintileValues
    mean_mae_atr20_by_quintile: QuintileValues
    maximum_stock_contribution: float | None
    top_five_stock_contribution: float | None
    lags: tuple[FactorLagDiagnostic, ...]
    strata: tuple[FactorStratumDiagnostic, ...]
    days: tuple[FactorDailyDiagnostic, ...]

    def __post_init__(self) -> None:
        if not self.factor_name.strip():
            raise ValueError("factor diagnostic name must not be empty")
        _counts(self.total_count, self.observed_count, self.coverage, self.missing_rate)
        if tuple(item.cost_rate for item in self.cost_quintiles) != _COST_RATES:
            raise ValueError("factor aggregate costs must remain 20bp, 50bp, and 100bp")
        if tuple(item.lag for item in self.lags) != _DECAY_LAGS:
            raise ValueError("factor aggregate lags must remain 1, 3, and 5 days")
        _optional_finite(
            (
                self.mean_ic,
                self.mean_rank_ic,
                self.icir,
                *self.severe_rate_by_quintile,
                *self.mean_mae_atr20_by_quintile,
                self.maximum_stock_contribution,
                self.top_five_stock_contribution,
            )
        )
        for value in (
            *self.severe_rate_by_quintile,
            self.maximum_stock_contribution,
            self.top_five_stock_contribution,
        ):
            _optional_rate(value)
        days = tuple(sorted(self.days, key=lambda item: item.trade_date))
        if len(days) > 40 or len({item.trade_date for item in days}) != len(days):
            raise ValueError("factor diagnostic accepts at most 40 unique days")
        if (
            sum(item.total_count for item in days) != self.total_count
            or sum(item.observed_count for item in days) != self.observed_count
        ):
            raise ValueError("factor aggregate counts must match its daily evidence")
        strata = tuple(sorted(self.strata, key=lambda item: (item.dimension, item.label)))
        if len({(item.dimension, item.label) for item in strata}) != len(strata):
            raise ValueError("factor diagnostic strata must be unique")
        object.__setattr__(self, "days", days)
        object.__setattr__(self, "strata", strata)


@dataclass(frozen=True)
class OracleRecallDay:
    trade_date: date
    oracle_count: int
    pre_pruning_recalled: int
    post_pruning_recalled: int
    pre_pruning_recall: float | None
    post_pruning_recall: float | None

    def __post_init__(self) -> None:
        if not 0 <= self.post_pruning_recalled <= self.pre_pruning_recalled <= self.oracle_count:
            raise ValueError("factor oracle recall counts are inconsistent")
        expected_pre = self.pre_pruning_recalled / self.oracle_count if self.oracle_count else None
        expected_post = self.post_pruning_recalled / self.oracle_count if self.oracle_count else None
        if self.pre_pruning_recall != expected_pre or self.post_pruning_recall != expected_post:
            raise ValueError("factor oracle recall rates must match their counts")


@dataclass(frozen=True)
class OracleRecallDiagnostic:
    oracle_count: int
    pre_pruning_recalled: int
    post_pruning_recalled: int
    pre_pruning_recall: float | None
    post_pruning_recall: float | None
    days: tuple[OracleRecallDay, ...]

    def __post_init__(self) -> None:
        days = tuple(sorted(self.days, key=lambda item: item.trade_date))
        if len(days) > 40 or len({item.trade_date for item in days}) != len(days):
            raise ValueError("factor oracle recall accepts at most 40 unique days")
        object.__setattr__(self, "days", days)
        if sum(item.oracle_count for item in self.days) != self.oracle_count:
            raise ValueError("factor oracle aggregate denominator is inconsistent")
        if sum(item.pre_pruning_recalled for item in self.days) != self.pre_pruning_recalled:
            raise ValueError("factor oracle pre-pruning numerator is inconsistent")
        if sum(item.post_pruning_recalled for item in self.days) != self.post_pruning_recalled:
            raise ValueError("factor oracle post-pruning numerator is inconsistent")
        expected_pre = self.pre_pruning_recalled / self.oracle_count if self.oracle_count else None
        expected_post = self.post_pruning_recalled / self.oracle_count if self.oracle_count else None
        if self.pre_pruning_recall != expected_pre or self.post_pruning_recall != expected_post:
            raise ValueError("factor oracle aggregate rates must match their counts")


@dataclass(frozen=True)
class ScoreFactorDiagnosticReport:
    status: DiagnosticStatus
    extraction_hash: str
    baseline_report_hash: str
    dimension_hash: str
    research_identity: str
    research_spec_hash: str
    factors: tuple[FactorAggregateDiagnostic, ...]
    oracle_recall: OracleRecallDiagnostic
    schema_version: str = "score_factor_diagnostic_report_v1"
    diagnostic_version: str = "score_native_factor_diagnostics_v1"
    cost_rates: tuple[float, float, float] = _COST_RATES
    decay_lags: tuple[int, int, int] = _DECAY_LAGS
    production_authority: bool = False
    report_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        for value, label in (
            (self.extraction_hash, "factor report extraction"),
            (self.baseline_report_hash, "factor report baseline"),
            (self.dimension_hash, "factor report dimensions"),
        ):
            _hash(value, label)
        spec = get_score_research_spec(self.research_identity)
        if self.research_spec_hash != spec.content_hash:
            raise ValueError("factor report research spec hash is invalid")
        if self.schema_version != "score_factor_diagnostic_report_v1" or self.diagnostic_version != (
            "score_native_factor_diagnostics_v1"
        ):
            raise ValueError("factor report implementation identity is invalid")
        if self.cost_rates != _COST_RATES or self.decay_lags != _DECAY_LAGS or self.production_authority is not False:
            raise ValueError("factor report fixed boundaries are invalid")
        factors = tuple(sorted(self.factors, key=lambda item: item.factor_name))
        if len({item.factor_name for item in factors}) != len(factors):
            raise ValueError("factor report names must be unique")
        day_identity = (
            tuple((item.trade_date, item.day_hash, item.input_hash) for item in factors[0].days) if factors else ()
        )
        if any(
            tuple((item.trade_date, item.day_hash, item.input_hash) for item in factor.days) != day_identity
            for factor in factors[1:]
        ):
            raise ValueError("factor report factors must share identical daily evidence")
        report_dates = (
            tuple(item[0] for item in day_identity)
            if factors
            else tuple(item.trade_date for item in self.oracle_recall.days)
        )
        if tuple(item.trade_date for item in self.oracle_recall.days) != report_dates:
            raise ValueError("factor report oracle recall must share the factor day coverage")
        day_count = len(report_dates)
        expected = "evaluated" if day_count == 40 else "exploratory"
        if self.status != expected:
            raise ValueError("factor report status must match its valid-day evidence")
        object.__setattr__(self, "factors", factors)
        object.__setattr__(self, "report_hash", canonical_hash(self))


def _counts(total: int, observed: int, coverage: float, missing_rate: float) -> None:
    if not 0 <= observed <= total:
        raise ValueError("factor diagnostic counts are inconsistent")
    expected = observed / total if total else 0.0
    if not math.isclose(coverage, expected) or not math.isclose(missing_rate, 1.0 - expected):
        raise ValueError("factor diagnostic coverage must match its counts")


def _optional_rate(value: float | None) -> None:
    if value is not None and not 0.0 <= value <= 1.0:
        raise ValueError("factor diagnostic rate must be in [0, 1]")


def _optional_finite(values: tuple[float | None, ...]) -> None:
    if any(value is not None and not math.isfinite(value) for value in values):
        raise ValueError("factor diagnostic values must be finite when present")


def _hash(value: str, label: str) -> None:
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be SHA-256")


def _code(code: str) -> None:
    if len(code) != 6 or not code.isdigit():
        raise ValueError("factor diagnostic code must contain exactly six digits")


__all__ = [
    "DiagnosticStatus",
    "FactorAggregateDiagnostic",
    "FactorCostQuintiles",
    "FactorDailyDiagnostic",
    "FactorDiagnosticDimensionRecord",
    "FactorDiagnosticDimensions",
    "FactorLagDiagnostic",
    "FactorStratumDiagnostic",
    "OracleRecallDay",
    "OracleRecallDiagnostic",
    "QuintileValues",
    "ScoreFactorDiagnosticReport",
]

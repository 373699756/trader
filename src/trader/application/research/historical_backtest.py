"""Aggregate fixed train/validation diagnostics from the historical bar archive."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Literal, Protocol

from trader.application.research.historical_screening import HistoricalArchiveManifest, HistoricalArchiveStatus
from trader.application.research.replay_models import canonical_hash
from trader.domain.research.historical_screening import HistoricalScreeningSpec


@dataclass(frozen=True)
class HistoricalScreeningDay:
    trade_date: date
    population: int
    selected: int
    selected_return_1d_pct: float
    selected_return_5d_pct: float
    benchmark_return_1d_pct: float
    benchmark_return_5d_pct: float
    severe_loss_rate: float


@dataclass(frozen=True)
class HistoricalSplitMetrics:
    trade_dates: int
    mean_population: float
    mean_selected: float
    mean_selected_return_1d_pct: float
    mean_selected_return_5d_pct: float
    mean_benchmark_return_1d_pct: float
    mean_benchmark_return_5d_pct: float
    mean_excess_return_1d_pct: float
    mean_excess_return_5d_pct: float
    severe_loss_rate: float


@dataclass(frozen=True)
class HistoricalBarBacktestReport:
    schema_version: str
    research_identity: str
    research_spec_hash: str
    status: Literal["screened", "insufficient_coverage"]
    archive: HistoricalArchiveStatus
    archive_manifest: HistoricalArchiveManifest
    training: HistoricalSplitMetrics
    validation: HistoricalSplitMetrics
    screening_version: str
    training_window: tuple[date, date]
    validation_window: tuple[date, date]
    round_trip_cost_bps: int
    promotion_authority: bool
    limitations: tuple[str, ...]
    report_hash: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "report_hash", canonical_hash(self))


class HistoricalScreeningEvidence(Protocol):
    def inspect(self, research_identity: str) -> HistoricalArchiveStatus: ...

    def manifest(self, spec: HistoricalScreeningSpec) -> HistoricalArchiveManifest: ...

    def screening_days(self, spec: HistoricalScreeningSpec) -> Sequence[HistoricalScreeningDay]: ...


class HistoricalBarBacktestService:
    def __init__(self, evidence: HistoricalScreeningEvidence, *, minimum_split_days: int = 100) -> None:
        if minimum_split_days < 1:
            raise ValueError("historical backtest minimum split days must be positive")
        self._evidence = evidence
        self._minimum_split_days = minimum_split_days

    def execute(self, spec: HistoricalScreeningSpec) -> HistoricalBarBacktestReport:
        archive = self._evidence.inspect(spec.research_identity)
        archive_manifest = self._evidence.manifest(spec)
        days = tuple(self._evidence.screening_days(spec))
        training = _metrics(tuple(item for item in days if spec.training_start <= item.trade_date <= spec.training_end))
        validation = _metrics(
            tuple(item for item in days if spec.validation_start <= item.trade_date <= spec.validation_end)
        )
        coverage_ratio = archive.completed_codes / archive.universe_count if archive.universe_count else 0.0
        ready = (
            archive.spec_hash == spec.content_hash
            and coverage_ratio >= 0.95
            and training.trade_dates >= self._minimum_split_days
            and validation.trade_dates >= self._minimum_split_days
        )
        return HistoricalBarBacktestReport(
            schema_version="score_h0_bar_screening",
            research_identity=spec.research_identity,
            research_spec_hash=spec.content_hash,
            status="screened" if ready else "insufficient_coverage",
            archive=archive,
            archive_manifest=archive_manifest,
            training=training,
            validation=validation,
            screening_version="ohlcv_cross_section",
            training_window=(spec.training_start, spec.training_end),
            validation_window=(spec.validation_start, spec.validation_end),
            round_trip_cost_bps=spec.round_trip_cost_bps,
            promotion_authority=False,
            limitations=(
                "current_universe_survivorship_bias",
                "historical_st_status_not_reconstructed",
                "historical_industry_not_reconstructed",
                "intraday_tail_not_reconstructed",
                "corporate_risk_not_reconstructed",
                "deepseek_facts_not_reconstructed",
            ),
        )


def _metrics(days: tuple[HistoricalScreeningDay, ...]) -> HistoricalSplitMetrics:
    if not days:
        return HistoricalSplitMetrics(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    count = len(days)
    selected_1d = _mean(tuple(item.selected_return_1d_pct for item in days))
    selected_5d = _mean(tuple(item.selected_return_5d_pct for item in days))
    benchmark_1d = _mean(tuple(item.benchmark_return_1d_pct for item in days))
    benchmark_5d = _mean(tuple(item.benchmark_return_5d_pct for item in days))
    return HistoricalSplitMetrics(
        trade_dates=count,
        mean_population=round(_mean(tuple(float(item.population) for item in days)), 4),
        mean_selected=round(_mean(tuple(float(item.selected) for item in days)), 4),
        mean_selected_return_1d_pct=round(selected_1d, 6),
        mean_selected_return_5d_pct=round(selected_5d, 6),
        mean_benchmark_return_1d_pct=round(benchmark_1d, 6),
        mean_benchmark_return_5d_pct=round(benchmark_5d, 6),
        mean_excess_return_1d_pct=round(selected_1d - benchmark_1d, 6),
        mean_excess_return_5d_pct=round(selected_5d - benchmark_5d, 6),
        severe_loss_rate=round(_mean(tuple(item.severe_loss_rate for item in days)), 6),
    )


def _mean(values: tuple[float, ...]) -> float:
    return sum(values) / len(values) if values else 0.0


__all__ = [
    "HistoricalBarBacktestReport",
    "HistoricalBarBacktestService",
    "HistoricalScreeningDay",
    "HistoricalSplitMetrics",
]

"""Complete Codex B orchestration for historical filter candidates."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Literal

from trader.domain.research.filter_recall_ablation import FilterAblationRow, run_filter_recall_ablation
from trader.domain.research.historical_candidate_confirmation import (
    CandidateConfirmationPlan,
    CandidateConfirmationSeries,
    HistoricalCandidateConfirmationReport,
    build_confirmation_folds,
    confirm_transparent_candidates,
    inherit_candidate_confirmation,
)
from trader.domain.research.transparent_candidate import (
    TransparentCandidate,
    TransparentCandidateFamily,
    TransparentCandidateMetrics,
    TransparentCandidateReport,
    evaluate_transparent_candidate,
    evaluate_transparent_candidate_family,
    preregister_transparent_candidates,
)

HistoricalStrategy = Literal["today", "tomorrow", "d25"]
_STRATEGIES: tuple[HistoricalStrategy, ...] = ("today", "tomorrow", "d25")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class HistoricalStrategyResearchRequest:
    strategy: HistoricalStrategy
    parent_split_hash: str
    parent_residual_ledger_hash: str
    development_dates: tuple[date, ...]
    confirmation_dates: tuple[date, ...]
    development_rows: tuple[FilterAblationRow, ...]
    confirmation_rows: tuple[FilterAblationRow, ...]
    removed_components: tuple[str, ...] = ()
    parent_status: Literal["ready", "historical_rejected", "historical_data_insufficient"] = "ready"
    parent_failure_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.strategy not in _STRATEGIES:
            raise ValueError("historical candidate strategy is invalid")
        if any(
            _SHA256.fullmatch(value) is None for value in (self.parent_split_hash, self.parent_residual_ledger_hash)
        ):
            raise ValueError("historical candidate parent hashes must be SHA-256")
        _ordered_dates(self.development_dates, "development")
        _ordered_dates(self.confirmation_dates, "confirmation")
        if self.development_dates[-1] >= self.confirmation_dates[0]:
            raise ValueError("historical candidate development must precede confirmation")
        if any(row.trade_date not in self.development_dates for row in self.development_rows):
            raise ValueError("historical candidate development row falls outside its sealed split")
        if any(row.trade_date not in self.confirmation_dates for row in self.confirmation_rows):
            raise ValueError("historical candidate confirmation row falls outside its sealed split")
        identities = tuple((row.trade_date, row.code) for row in (*self.development_rows, *self.confirmation_rows))
        if len(identities) != len(set(identities)):
            raise ValueError("historical candidate rows must be unique across sealed splits")
        if self.parent_status == "ready" and self.parent_failure_reasons:
            raise ValueError("ready historical candidate parent cannot contain failure reasons")
        if self.parent_status != "ready" and (not self.parent_failure_reasons or identities):
            raise ValueError("failed historical candidate parent requires reasons and cannot supply research rows")


@dataclass(frozen=True)
class HistoricalStrategyResearchResult:
    strategy: HistoricalStrategy
    parent_split_hash: str
    parent_residual_ledger_hash: str
    ablation_report_hash: str
    candidate_report: TransparentCandidateReport
    confirmation_report: HistoricalCandidateConfirmationReport
    status: Literal["historical_candidate_ready", "historical_rejected", "historical_data_insufficient"]
    terminal_holdout_status: Literal["terminal_holdout_not_opened"] = "terminal_holdout_not_opened"
    production_authority: bool = False
    schema_version: str = "historical_strategy_candidate_result_v1"
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        if self.strategy not in _STRATEGIES:
            raise ValueError("historical strategy result identity is invalid")
        hashes = (self.parent_split_hash, self.parent_residual_ledger_hash, self.ablation_report_hash)
        if any(_SHA256.fullmatch(value) is None for value in hashes):
            raise ValueError("historical strategy result parent hash is invalid")
        if self.status != self.confirmation_report.status:
            raise ValueError("historical strategy result must preserve confirmation terminal status")
        if (
            self.production_authority
            or self.terminal_holdout_status != "terminal_holdout_not_opened"
            or self.schema_version != "historical_strategy_candidate_result_v1"
        ):
            raise ValueError("historical strategy result cannot authorize production or open holdout")
        object.__setattr__(self, "content_hash", _canonical_hash(self))


@dataclass(frozen=True)
class HistoricalCodexBBatchResult:
    strategies: tuple[HistoricalStrategyResearchResult, ...]
    production_authority: bool = False
    schema_version: str = "historical_codex_b_batch_result_v1"
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        if tuple(item.strategy for item in self.strategies) != _STRATEGIES:
            raise ValueError("Codex B batch must contain Today, Tomorrow, and D25 in fixed order")
        if self.production_authority or self.schema_version != "historical_codex_b_batch_result_v1":
            raise ValueError("Codex B batch cannot authorize production")
        object.__setattr__(self, "content_hash", _canonical_hash(self))


def execute_historical_candidate_confirmation(
    family: TransparentCandidateFamily,
    development_series: tuple[CandidateConfirmationSeries, ...],
    confirmation_series: tuple[CandidateConfirmationSeries, ...],
    *,
    selected_candidate_id: str,
) -> HistoricalCandidateConfirmationReport:
    """Evaluate a sealed development family and its sole confirmation candidate."""

    return confirm_transparent_candidates(
        family,
        development_series,
        confirmation_series,
        CandidateConfirmationPlan(selected_candidate_id),
    )


def execute_historical_strategy_research(
    request: HistoricalStrategyResearchRequest,
    *,
    repetitions: int = 10_000,
) -> HistoricalStrategyResearchResult:
    ablation = run_filter_recall_ablation(
        request.development_rows,
        strategy=request.strategy,
        development_dates=request.development_dates,
    )
    family = preregister_transparent_candidates(ablation, removed_components=request.removed_components)
    candidate_report = evaluate_transparent_candidate_family(family, request.development_rows)
    if request.parent_status == "ready" and candidate_report.status == "candidate_family_sealed":
        selected_candidate_id = candidate_report.selected_candidate_id
        if selected_candidate_id is None:
            raise AssertionError("sealed candidate family requires one selected candidate")
        candidates = {candidate.candidate_id: candidate for candidate in family.candidates}
        development_series = _candidate_series(
            family.candidates,
            rows=request.development_rows,
            dates=request.development_dates,
            direction_rows=request.development_rows,
            direction_dates=request.development_dates,
        )
        confirmation_series = _candidate_series(
            (family.candidates[0], candidates[selected_candidate_id]),
            rows=request.confirmation_rows,
            dates=request.confirmation_dates,
        )
        confirmation = confirm_transparent_candidates(
            family,
            development_series,
            confirmation_series,
            CandidateConfirmationPlan(selected_candidate_id, repetitions=repetitions),
        )
    elif request.parent_status != "ready":
        confirmation = inherit_candidate_confirmation(
            family,
            confirmation_dates=request.confirmation_dates,
            status=request.parent_status,
            failure_reasons=request.parent_failure_reasons,
        )
    else:
        inherited_status: Literal["historical_rejected", "historical_data_insufficient"] = (
            "historical_data_insufficient"
            if candidate_report.status == "historical_data_insufficient"
            else "historical_rejected"
        )
        reason = (
            "development_data_insufficient"
            if inherited_status == "historical_data_insufficient"
            else "development_candidate_not_selected"
        )
        confirmation = inherit_candidate_confirmation(
            family,
            confirmation_dates=request.confirmation_dates,
            status=inherited_status,
            failure_reasons=(reason,),
        )
    return HistoricalStrategyResearchResult(
        strategy=request.strategy,
        parent_split_hash=request.parent_split_hash,
        parent_residual_ledger_hash=request.parent_residual_ledger_hash,
        ablation_report_hash=ablation.content_hash,
        candidate_report=candidate_report,
        confirmation_report=confirmation,
        status=confirmation.status,
    )


def execute_codex_b_batch(
    requests: tuple[HistoricalStrategyResearchRequest, ...],
    *,
    repetitions: int = 10_000,
) -> HistoricalCodexBBatchResult:
    by_strategy = {request.strategy: request for request in requests}
    if len(requests) != len(_STRATEGIES) or set(by_strategy) != set(_STRATEGIES):
        raise ValueError("Codex B batch requires exactly one request for each strategy")
    return HistoricalCodexBBatchResult(
        tuple(
            execute_historical_strategy_research(by_strategy[strategy], repetitions=repetitions)
            for strategy in _STRATEGIES
        )
    )


def _candidate_series(
    candidates: tuple[TransparentCandidate, ...],
    *,
    rows: tuple[FilterAblationRow, ...],
    dates: tuple[date, ...],
    direction_rows: tuple[FilterAblationRow, ...] = (),
    direction_dates: tuple[date, ...] = (),
) -> tuple[CandidateConfirmationSeries, ...]:
    control = candidates[0]
    control_daily = tuple(_daily_metrics(control, rows, trade_date) for trade_date in dates)
    folds = build_confirmation_folds(direction_dates) if direction_dates else ()
    result: list[CandidateConfirmationSeries] = []
    for candidate in candidates:
        candidate_daily = tuple(_daily_metrics(candidate, rows, trade_date) for trade_date in dates)
        fold_directions = tuple(_fold_direction(candidate, control, direction_rows, fold) for fold in folds)
        result.append(
            CandidateConfirmationSeries(
                candidate_id=candidate.candidate_id,
                trade_dates=dates,
                paired_increment_20bp=tuple(
                    item.net_excess_20bp - baseline.net_excess_20bp
                    for item, baseline in zip(candidate_daily, control_daily, strict=True)
                ),
                paired_increment_50bp=tuple(
                    item.net_excess_50bp - baseline.net_excess_50bp
                    for item, baseline in zip(candidate_daily, control_daily, strict=True)
                ),
                severe_loss_rate_delta=tuple(
                    item.severe_loss_rate - baseline.severe_loss_rate
                    for item, baseline in zip(candidate_daily, control_daily, strict=True)
                ),
                turnover_delta=tuple(
                    item.turnover - baseline.turnover
                    for item, baseline in zip(candidate_daily, control_daily, strict=True)
                ),
                capacity_delta=tuple(
                    item.capacity - baseline.capacity
                    for item, baseline in zip(candidate_daily, control_daily, strict=True)
                ),
                concentration_delta=tuple(
                    item.concentration - baseline.concentration
                    for item, baseline in zip(candidate_daily, control_daily, strict=True)
                ),
                candidate_net_excess_20bp=tuple(item.net_excess_20bp for item in candidate_daily),
                candidate_net_excess_50bp=tuple(item.net_excess_50bp for item in candidate_daily),
                development_fold_directions=fold_directions,
            )
        )
    return tuple(result)


def _daily_metrics(
    candidate: TransparentCandidate,
    rows: tuple[FilterAblationRow, ...],
    trade_date: date,
) -> TransparentCandidateMetrics:
    daily = tuple(row for row in rows if row.trade_date == trade_date)
    return evaluate_transparent_candidate(candidate, daily, evaluation_dates=(trade_date,))


def _fold_direction(
    candidate: TransparentCandidate,
    control: TransparentCandidate,
    rows: tuple[FilterAblationRow, ...],
    fold: tuple[date, ...],
) -> int:
    fold_rows = tuple(row for row in rows if row.trade_date in fold)
    candidate_metrics = evaluate_transparent_candidate(candidate, fold_rows, evaluation_dates=fold)
    control_metrics = evaluate_transparent_candidate(control, fold_rows, evaluation_dates=fold)
    increment = candidate_metrics.net_excess_20bp - control_metrics.net_excess_20bp
    return 1 if increment > 0.0 else (-1 if increment < 0.0 else 0)


def _ordered_dates(values: tuple[date, ...], label: str) -> None:
    if len(values) < 5 or tuple(sorted(set(values))) != values:
        raise ValueError(f"historical candidate {label} dates must contain at least five ordered sessions")


def _canonical_hash(value: object) -> str:
    def encode(item: object) -> object:
        if dataclasses.is_dataclass(item):
            return {field.name: encode(getattr(item, field.name)) for field in dataclasses.fields(item) if field.init}
        if isinstance(item, date):
            return item.isoformat()
        if isinstance(item, (frozenset, set)):
            return [encode(child) for child in sorted(item)]
        if isinstance(item, (tuple, list)):
            return [encode(child) for child in item]
        if isinstance(item, dict):
            return {str(key): encode(child) for key, child in item.items()}
        return item

    payload = json.dumps(encode(value), ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()


__all__ = [
    "HistoricalCodexBBatchResult",
    "HistoricalStrategyResearchRequest",
    "HistoricalStrategyResearchResult",
    "execute_codex_b_batch",
    "execute_historical_candidate_confirmation",
    "execute_historical_strategy_research",
]

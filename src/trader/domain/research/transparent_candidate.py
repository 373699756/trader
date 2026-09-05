"""Preregistered, human-readable candidate families for historical research."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import date
from typing import Literal

from trader.domain.research.filter_recall_ablation import (
    ABLATION_RULES,
    FilterAblationRow,
    FilterRecallAblationReport,
    FilterRuleId,
)

CandidateChangeKind = Literal["control", "observe_rule", "remove_component", "cost_guard"]
_IDENTITY = re.compile(r"^[a-z0-9_]{1,96}$")


@dataclass(frozen=True)
class TransparentCandidate:
    candidate_id: str
    strategy: str
    change_kind: CandidateChangeKind
    disabled_rules: frozenset[FilterRuleId] = frozenset()
    removed_component: str | None = None
    cost_rate: float = 0.002
    severe_loss_guard: float | None = None
    rationale: str = ""
    production_authority: bool = False
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        _validate_candidate_change(self)
        _validate_candidate_risk(self)
        object.__setattr__(self, "content_hash", _hash(self))


def _validate_candidate_change(candidate: TransparentCandidate) -> None:
    if _IDENTITY.fullmatch(candidate.candidate_id) is None or not candidate.strategy:
        raise ValueError("transparent candidate identity is invalid")
    if candidate.change_kind not in {"control", "observe_rule", "remove_component", "cost_guard"}:
        raise ValueError("transparent candidate change kind is invalid")
    if any(rule not in ABLATION_RULES for rule in candidate.disabled_rules):
        raise ValueError("transparent candidate disabled rule is not preregistered")
    if candidate.change_kind == "control" and (
        candidate.disabled_rules or candidate.removed_component or candidate.severe_loss_guard is not None
    ):
        raise ValueError("control candidate cannot change the production chain")
    if candidate.change_kind == "observe_rule" and len(candidate.disabled_rules) != 1:
        raise ValueError("observe candidate must disable exactly one rule")
    if candidate.change_kind == "remove_component" and (not candidate.removed_component or candidate.disabled_rules):
        raise ValueError("component candidate must name exactly one component")
    if candidate.change_kind == "cost_guard" and (
        candidate.severe_loss_guard is None or candidate.disabled_rules or candidate.removed_component
    ):
        raise ValueError("cost guard candidate requires one fixed guard")


def _validate_candidate_risk(candidate: TransparentCandidate) -> None:
    if not math.isfinite(candidate.cost_rate) or candidate.cost_rate not in {0.002, 0.005, 0.01}:
        raise ValueError("transparent candidate cost must be 20bp, 50bp, or 100bp")
    if candidate.severe_loss_guard is not None and (
        not math.isfinite(candidate.severe_loss_guard) or candidate.severe_loss_guard < 0.0
    ):
        raise ValueError("transparent candidate severe-loss guard is invalid")
    if candidate.production_authority:
        raise ValueError("transparent candidates cannot authorize production")


@dataclass(frozen=True)
class TransparentCandidateMetrics:
    candidate_id: str
    evaluated_rows: int
    evaluated_dates: int
    net_excess_20bp: float
    net_excess_50bp: float
    severe_loss_rate: float
    turnover: float
    capacity: float
    concentration: float
    profitable_executable_recall: float | None
    implementation_complexity: int
    profitable_executable_recall_50bp: float | None = None
    mean_mae_atr20: float | None = None
    maximum_board_fraction: float = 0.0
    maximum_industry_fraction: float = 0.0
    maximum_stock_positive_fraction: float = 0.0
    top_five_positive_fraction: float = 0.0
    maximum_date_positive_fraction: float = 0.0
    daily_net_excess_20bp: tuple[float, ...] = ()
    daily_net_excess_50bp: tuple[float, ...] = ()

    def __post_init__(self) -> None:
        if (
            _IDENTITY.fullmatch(self.candidate_id) is None
            or min(self.evaluated_rows, self.evaluated_dates, self.implementation_complexity) < 0
        ):
            raise ValueError("transparent candidate metrics identity/count is invalid")
        values = (
            self.net_excess_20bp,
            self.net_excess_50bp,
            self.severe_loss_rate,
            self.turnover,
            self.capacity,
            self.concentration,
            self.maximum_board_fraction,
            self.maximum_industry_fraction,
            self.maximum_stock_positive_fraction,
            self.top_five_positive_fraction,
            self.maximum_date_positive_fraction,
            *self.daily_net_excess_20bp,
            *self.daily_net_excess_50bp,
        )
        if any(not math.isfinite(value) for value in values):
            raise ValueError("transparent candidate metrics must be finite")
        rates = (
            self.severe_loss_rate,
            self.turnover,
            self.concentration,
            self.maximum_board_fraction,
            self.maximum_industry_fraction,
            self.maximum_stock_positive_fraction,
            self.top_five_positive_fraction,
            self.maximum_date_positive_fraction,
        )
        if any(not 0.0 <= value <= 1.0 for value in rates):
            raise ValueError("transparent candidate rates must be in [0, 1]")
        recalls = (self.profitable_executable_recall, self.profitable_executable_recall_50bp)
        if any(value is not None and not 0.0 <= value <= 1.0 for value in recalls):
            raise ValueError("transparent candidate recall must be in [0, 1]")
        if self.mean_mae_atr20 is not None and not math.isfinite(self.mean_mae_atr20):
            raise ValueError("transparent candidate MAE must be finite")
        if len(self.daily_net_excess_20bp) != len(self.daily_net_excess_50bp):
            raise ValueError("transparent candidate daily cost evidence must align")


@dataclass(frozen=True)
class TransparentCandidateFamily:
    strategy: str
    candidates: tuple[TransparentCandidate, ...]
    source_ablation_hash: str
    development_dates: tuple[date, ...]
    confirmation_locked: bool = False
    production_authority: bool = False
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        if not self.strategy or not self.candidates or len(self.candidates) > 8:
            raise ValueError("transparent candidate family must contain one to eight candidates")
        if self.candidates[0].change_kind != "control":
            raise ValueError("transparent candidate family must start with its control")
        if len({candidate.candidate_id for candidate in self.candidates}) != len(self.candidates):
            raise ValueError("transparent candidate ids must be unique")
        if not re.fullmatch(r"[0-9a-f]{64}", self.source_ablation_hash) or not self.development_dates:
            raise ValueError("transparent candidate family identity is invalid")
        if self.confirmation_locked and self.production_authority:
            raise ValueError("locked research family cannot authorize production")
        if self.production_authority:
            raise ValueError("transparent candidate family cannot authorize production")
        object.__setattr__(self, "content_hash", _hash(self))


@dataclass(frozen=True)
class TransparentCandidateReport:
    family: TransparentCandidateFamily
    metrics: tuple[TransparentCandidateMetrics, ...]
    selected_candidate_id: str | None
    status: Literal["candidate_family_sealed", "historical_rejected", "historical_data_insufficient"]
    production_authority: bool = False
    schema_version: str = "transparent_candidate_report"
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        expected = tuple(candidate.candidate_id for candidate in self.family.candidates)
        if tuple(item.candidate_id for item in self.metrics) != expected:
            raise ValueError("transparent candidate report must retain the complete family")
        if self.status == "candidate_family_sealed" and self.selected_candidate_id not in expected:
            raise ValueError("sealed transparent family requires one registered candidate")
        if self.status != "candidate_family_sealed" and self.selected_candidate_id is not None:
            raise ValueError("rejected transparent family cannot select a candidate")
        if self.production_authority or self.schema_version != "transparent_candidate_report":
            raise ValueError("transparent candidate report cannot authorize production")
        object.__setattr__(self, "content_hash", _hash(self))


def preregister_transparent_candidates(
    report: FilterRecallAblationReport,
    *,
    removed_components: tuple[str, ...] = (),
    include_cost_guard: bool = True,
) -> TransparentCandidateFamily:
    """Build at most eight candidates from the sealed development ablation."""

    candidates: list[TransparentCandidate] = [
        TransparentCandidate(f"{report.strategy}_control", report.strategy, "control")
    ]
    for rule in report.recommendations:
        if len(candidates) >= 8:
            break
        if rule in ABLATION_RULES:
            candidates.append(
                TransparentCandidate(
                    f"{report.strategy}_observe_{rule}",
                    report.strategy,
                    "observe_rule",
                    frozenset({rule}),
                    rationale="ablation_recall",
                )
            )
    for component in removed_components:
        if len(candidates) >= 8:
            break
        if not component.strip():
            raise ValueError("removed component must be named")
        candidates.append(
            TransparentCandidate(
                f"{report.strategy}_remove_{component}",
                report.strategy,
                "remove_component",
                removed_component=component,
                rationale="stable_zero_component",
            )
        )
    if include_cost_guard and len(candidates) < 8:
        candidates.append(
            TransparentCandidate(
                f"{report.strategy}_cost_guard",
                report.strategy,
                "cost_guard",
                severe_loss_guard=0.10,
                rationale="registered_cost_guard",
            )
        )
    return TransparentCandidateFamily(report.strategy, tuple(candidates), report.content_hash, report.development_dates)


def evaluate_transparent_candidate(
    candidate: TransparentCandidate,
    rows: tuple[FilterAblationRow, ...],
    *,
    control: TransparentCandidateMetrics | None = None,
    evaluation_dates: tuple[date, ...] | None = None,
) -> TransparentCandidateMetrics:
    """Evaluate one fixed candidate without fitting or searching parameters."""

    if not rows and not evaluation_dates:
        raise ValueError("transparent candidate evaluation requires rows")
    if candidate.removed_component is not None and any(
        row.permanent_eligible
        and not row.safety_veto
        and candidate.removed_component not in {item.name for item in row.score_components}
        for row in rows
    ):
        raise ValueError("transparent component candidate requires complete component evidence")
    eligible = tuple(row for row in rows if _candidate_passes(candidate, row))
    if candidate.severe_loss_guard is not None:
        eligible = tuple(
            row
            for row in eligible
            if row.predicted_severe_loss_risk is not None
            and row.predicted_severe_loss_risk <= candidate.severe_loss_guard
        )
    dates = tuple(sorted(set(evaluation_dates or tuple(row.trade_date for row in rows))))
    if any(row.trade_date not in dates for row in rows):
        raise ValueError("transparent candidate rows fall outside the evaluation dates")
    daily_20 = tuple(_daily_return(eligible, trade_date, cost="20bp") for trade_date in dates)
    daily_50 = tuple(_daily_return(eligible, trade_date, cost="50bp") for trade_date in dates)
    severe = tuple(row.severe_loss for row in eligible)
    denominator = sum(row.eligible_profit for row in rows)
    denominator_50 = sum(row.eligible_profit_50bp for row in rows)
    recall = None if denominator == 0 else sum(row.eligible_profit for row in eligible) / denominator
    recall_50 = None if denominator_50 == 0 else sum(row.eligible_profit_50bp for row in eligible) / denominator_50
    selections = tuple(frozenset(row.code for row in eligible if row.trade_date == trade_date) for trade_date in dates)
    turnover = _turnover(selections)
    board_fraction, industry_fraction = _selection_concentration(eligible, dates)
    stock_fraction, top_five_fraction, date_fraction = _positive_concentration(eligible)
    return TransparentCandidateMetrics(
        candidate.candidate_id,
        len(eligible),
        len(dates),
        math.fsum(daily_20) / len(daily_20),
        math.fsum(daily_50) / len(daily_50),
        sum(severe) / len(severe) if severe else 0.0,
        turnover,
        min((row.capacity for row in eligible), default=0.0),
        max(board_fraction, industry_fraction),
        recall,
        1 if candidate.change_kind == "control" else 2,
        profitable_executable_recall_50bp=recall_50,
        mean_mae_atr20=(math.fsum(row.mae_atr20 for row in eligible) / len(eligible) if eligible else None),
        maximum_board_fraction=board_fraction,
        maximum_industry_fraction=industry_fraction,
        maximum_stock_positive_fraction=stock_fraction,
        top_five_positive_fraction=top_five_fraction,
        maximum_date_positive_fraction=date_fraction,
        daily_net_excess_20bp=daily_20,
        daily_net_excess_50bp=daily_50,
    )


def evaluate_transparent_candidate_family(
    family: TransparentCandidateFamily,
    rows: tuple[FilterAblationRow, ...],
) -> TransparentCandidateReport:
    if family.confirmation_locked:
        raise ValueError("transparent candidate family cannot be reevaluated after confirmation is locked")
    metrics = tuple(
        evaluate_transparent_candidate(candidate, rows, evaluation_dates=family.development_dates)
        for candidate in family.candidates
    )
    selected = choose_development_candidate(metrics)
    if not any(item.evaluated_rows for item in metrics):
        status: Literal["candidate_family_sealed", "historical_rejected", "historical_data_insufficient"] = (
            "historical_data_insufficient"
        )
    else:
        status = "candidate_family_sealed" if selected is not None else "historical_rejected"
    return TransparentCandidateReport(
        family=family,
        metrics=metrics,
        selected_candidate_id=selected,
        status=status,
    )


def choose_development_candidate(metrics: tuple[TransparentCandidateMetrics, ...]) -> str | None:
    """Choose a sole candidate only when it clears fixed risk and concentration gates."""

    if not metrics or metrics[0].candidate_id == "":
        return None
    control = metrics[0]
    eligible = tuple(
        item
        for item in metrics[1:]
        if item.net_excess_20bp > control.net_excess_20bp
        and item.net_excess_50bp > control.net_excess_50bp
        and item.severe_loss_rate <= control.severe_loss_rate
        and item.turnover <= control.turnover
        and item.capacity >= control.capacity
        and item.concentration <= control.concentration
        and item.maximum_stock_positive_fraction <= 0.10
        and item.top_five_positive_fraction <= 0.30
        and item.maximum_date_positive_fraction <= 0.10
    )
    return (
        max(
            eligible,
            key=lambda item: (
                item.net_excess_20bp,
                item.net_excess_50bp,
                -item.implementation_complexity,
                item.candidate_id,
            ),
        ).candidate_id
        if eligible
        else None
    )


def _daily_return(
    rows: tuple[FilterAblationRow, ...],
    trade_date: date,
    *,
    cost: Literal["20bp", "50bp"],
) -> float:
    selected = tuple(row for row in rows if row.trade_date == trade_date)
    if not selected:
        return 0.0
    values = tuple(row.actual_net_excess_20bp if cost == "20bp" else row.actual_net_excess_50bp for row in selected)
    return math.fsum(values) / len(values)


def _candidate_passes(candidate: TransparentCandidate, row: FilterAblationRow) -> bool:
    if (
        not row.permanent_eligible
        or row.safety_veto
        or not row.local_action_passed
        or not row.topk_concentration_passed
    ):
        return False
    blockers = tuple(rule for rule in row.matched_rules if rule in ABLATION_RULES and rule != "candidate_score")
    if any(rule not in candidate.disabled_rules for rule in blockers):
        return False
    candidate_score = row.candidate_score
    if candidate.removed_component is not None:
        remaining = tuple(item for item in row.score_components if item.name != candidate.removed_component)
        remaining_weight = math.fsum(item.weight for item in remaining)
        if not remaining or remaining_weight <= 0.0:
            return False
        candidate_score = math.fsum(item.value * item.weight for item in remaining) / remaining_weight
    if "candidate_score" not in candidate.disabled_rules and (candidate_score is None or candidate_score < 50.0):
        return False
    return True


def _turnover(selections: tuple[frozenset[str], ...]) -> float:
    if len(selections) < 2:
        return 0.0
    values: list[float] = []
    for previous, current in zip(selections, selections[1:], strict=False):
        denominator = max(len(previous), len(current), 1)
        values.append(len(previous.symmetric_difference(current)) / (2 * denominator))
    return math.fsum(values) / len(values)


def _selection_concentration(rows: tuple[FilterAblationRow, ...], dates: tuple[date, ...]) -> tuple[float, float]:
    maximum_board = maximum_industry = 0.0
    for trade_date in dates:
        selected = tuple(row for row in rows if row.trade_date == trade_date)
        if not selected:
            continue
        for field in ("board", "industry"):
            counts: dict[str, int] = {}
            for row in selected:
                key = getattr(row, field)
                counts[key] = counts.get(key, 0) + 1
            fraction = max(counts.values()) / len(selected)
            if field == "board":
                maximum_board = max(maximum_board, fraction)
            else:
                maximum_industry = max(maximum_industry, fraction)
    return maximum_board, maximum_industry


def _positive_concentration(rows: tuple[FilterAblationRow, ...]) -> tuple[float, float, float]:
    by_stock: dict[str, float] = {}
    by_date: dict[date, float] = {}
    for row in rows:
        positive = max(0.0, row.actual_net_excess_20bp)
        by_stock[row.code] = by_stock.get(row.code, 0.0) + positive
        by_date[row.trade_date] = by_date.get(row.trade_date, 0.0) + positive
    total = math.fsum(by_stock.values())
    if total <= 0.0:
        return 0.0, 0.0, 0.0
    stock_shares = sorted((value / total for value in by_stock.values()), reverse=True)
    date_shares = tuple(value / total for value in by_date.values())
    return stock_shares[0], math.fsum(stock_shares[:5]), max(date_shares)


def _hash(value: object) -> str:
    def encode(item: object) -> object:
        if dataclasses.is_dataclass(item):
            return {field.name: encode(getattr(item, field.name)) for field in dataclasses.fields(item) if field.init}
        if isinstance(item, (date,)):
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
    "CandidateChangeKind",
    "TransparentCandidate",
    "TransparentCandidateFamily",
    "TransparentCandidateMetrics",
    "TransparentCandidateReport",
    "choose_development_candidate",
    "evaluate_transparent_candidate",
    "evaluate_transparent_candidate_family",
    "preregister_transparent_candidates",
]

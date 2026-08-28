"""Immutable application records for cost-aware shadow selection."""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass
from datetime import date
from typing import Literal

from trader.application.research.replay_models import canonical_hash
from trader.application.research.shadow_model_models import ShadowHorizon, ShadowModelFamily, ShadowWindowMode
from trader.domain.research.cost_aware_selection import CostAwareEvaluation

_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class CostAwareSelectionDay:
    prediction_date: date
    horizon: ShadowHorizon
    window_mode: ShadowWindowMode
    model_family: ShadowModelFamily
    evaluations: tuple[CostAwareEvaluation, ...]
    selected_codes: tuple[str, ...]
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        evaluations = tuple(self.evaluations)
        codes = tuple(item.code for item in evaluations)
        if len(codes) != len(set(codes)):
            raise ValueError("cost-aware day evaluations must contain unique codes")
        ranked = tuple(item for item in evaluations if item.selected_rank is not None)
        if tuple(item.code for item in sorted(ranked, key=lambda item: item.selected_rank or 0)) != self.selected_codes:
            raise ValueError("cost-aware selected codes must match evaluation ranks")
        if tuple(sorted(item.selected_rank for item in ranked if item.selected_rank is not None)) != tuple(
            range(1, len(ranked) + 1)
        ):
            raise ValueError("cost-aware selected ranks must be contiguous")
        if len(ranked) > 6:
            raise ValueError("cost-aware selection cannot exceed Top6")
        if ranked:
            boards = {item.board for item in ranked}
            if max(sum(item.board == board for item in ranked) / len(ranked) for board in boards) > 0.60:
                raise ValueError("cost-aware selection exceeds the board concentration limit")
            industries = {item.industry for item in ranked}
            if max(sum(item.industry == industry for item in ranked) for industry in industries) > 2:
                raise ValueError("cost-aware selection exceeds the industry concentration limit")
        object.__setattr__(self, "evaluations", evaluations)
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class CostAwareSelectionReport:
    parent_report_hash: str
    parent_spec_hash: str
    selection_spec_hash: str
    top_k: int
    maximum_per_industry: int
    maximum_board_fraction: float
    d25_entry_threshold: float
    d25_maintenance_threshold: float
    tomorrow_entry_threshold: float
    days: tuple[CostAwareSelectionDay, ...]
    status: Literal["exploratory"] = "exploratory"
    production_authority: bool = False
    schema_version: str = "score_tomorrow_cost_aware_selection_report_v1"
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        for value in (self.parent_report_hash, self.parent_spec_hash, self.selection_spec_hash):
            if _SHA256.fullmatch(value) is None:
                raise ValueError("cost-aware report identities must be SHA-256")
        if (
            self.top_k != 6
            or self.maximum_per_industry != 2
            or self.maximum_board_fraction != 0.60
            or self.d25_entry_threshold != 0.002
            or self.d25_maintenance_threshold != 0.0
            or self.tomorrow_entry_threshold != 0.0
        ):
            raise ValueError("cost-aware report policy differs from the frozen specification")
        if self.status != "exploratory" or self.production_authority:
            raise ValueError("cost-aware report cannot authorize production")
        days = tuple(sorted(self.days, key=_day_order))
        identities = tuple(_day_identity(item) for item in days)
        if len(identities) != len(set(identities)):
            raise ValueError("cost-aware report contains duplicate day identities")
        base_families: dict[tuple[date, ShadowHorizon, ShadowWindowMode], set[ShadowModelFamily]] = {}
        for day in days:
            base_families.setdefault((day.prediction_date, day.horizon, day.window_mode), set()).add(day.model_family)
        if any(families != {"linear", "lightgbm"} for families in base_families.values()):
            raise ValueError("cost-aware report requires both model families for every shadow fold")
        if days and {item.horizon for item in days} != {"tomorrow", "d25"}:
            raise ValueError("cost-aware report must contain both horizons")
        object.__setattr__(self, "days", days)
        object.__setattr__(self, "content_hash", canonical_hash(self))


def _day_identity(day: CostAwareSelectionDay) -> tuple[date, ShadowHorizon, ShadowWindowMode, ShadowModelFamily]:
    return (day.prediction_date, day.horizon, day.window_mode, day.model_family)


def _day_order(day: CostAwareSelectionDay) -> tuple[date, str, str, str]:
    return (day.prediction_date, day.horizon, day.window_mode, day.model_family)


__all__ = ["CostAwareSelectionDay", "CostAwareSelectionReport"]

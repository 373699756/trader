"""Pure training labels and isolated chronological date windows."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from trader.infra.serialization.canonical_codec import CanonicalDigest

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
MAX_TRAINING_SESSIONS = 2000
TRAINING_POINT_IN_TIME_RESERVE = 200
MIN_TRAINING_DATES = 1250


@dataclass(frozen=True)
class TrainingLabelContract:
    formula: str = "(close[D+1]/close[D]-1)-eligible_universe_equal_weight_return[D+1]"
    primary_cost_bps: int = 20
    gate_cost_bps: int = 50
    stress_cost_bps: int = 100
    label_pending_required: bool = True
    schema_version: str = "pre_cost_next_day_excess_label"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.formula != "(close[D+1]/close[D]-1)-eligible_universe_equal_weight_return[D+1]":
            raise ValueError("training label formula is fixed")
        if (self.primary_cost_bps, self.gate_cost_bps, self.stress_cost_bps) != (20, 50, 100):
            raise ValueError("training label costs are fixed")
        if not self.label_pending_required or self.schema_version != "pre_cost_next_day_excess_label":
            raise ValueError("training label contract is invalid")
        object.__setattr__(self, "content_hash", CanonicalDigest(self))


@dataclass(frozen=True)
class TrainingDateSplit:
    parent_manifest_hash: str
    label_contract: TrainingLabelContract
    model_fit_dates: tuple[date, ...]
    warmup_dates: tuple[date, ...]
    fit_embargo_dates: tuple[date, ...]
    early_stopping_dates: tuple[date, ...]
    calibration_embargo_dates: tuple[date, ...]
    calibration_dates: tuple[date, ...]
    development_dates: tuple[date, ...]
    first_embargo_dates: tuple[date, ...]
    confirmation_dates: tuple[date, ...]
    second_embargo_dates: tuple[date, ...]
    daily_proxy_holdout_dates: tuple[date, ...]
    terminal_embargo_dates: tuple[date, ...]
    point_in_time_holdout_dates: tuple[date, ...]
    training_anchor: str = "15:00_daily_close"
    point_in_time_parity: bool = False
    terminal_holdout_opened: bool = False
    production_authority: bool = False
    schema_version: str = "training_date_split"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if _SHA256.fullmatch(self.parent_manifest_hash) is None:
            raise ValueError("training split parent hash is invalid")
        _validate_training_split_dates(self)
        if self.training_anchor != "15:00_daily_close" or self.point_in_time_parity:
            raise ValueError("training split must remain a daily-close proxy")
        if self.terminal_holdout_opened or self.production_authority:
            raise ValueError("training split cannot open holdouts or authorize production")
        if self.schema_version not in {"training_date_split", "v5_recent_training_split"}:
            raise ValueError("training split schema is invalid")
        object.__setattr__(self, "content_hash", CanonicalDigest(self))


def _validate_training_split_dates(value: TrainingDateSplit) -> None:
    ordered_groups = (
        value.warmup_dates,
        value.development_dates,
        value.first_embargo_dates,
        value.confirmation_dates,
        value.second_embargo_dates,
        value.daily_proxy_holdout_dates,
        value.terminal_embargo_dates,
        value.point_in_time_holdout_dates,
    )
    flattened = tuple(day for group in ordered_groups for day in group)
    if flattened != tuple(sorted(set(flattened))):
        raise ValueError("training split dates must be unique and chronological")
    recent = value.schema_version == "v5_recent_training_split"
    if (len(value.model_fit_dates) < 61 if recent else len(value.development_dates) < 600) or len(
        value.confirmation_dates
    ) < 200:
        raise ValueError("training development or confirmation dates are insufficient")
    if len(value.daily_proxy_holdout_dates) < 200 or len(value.point_in_time_holdout_dates) != 200:
        raise ValueError("training holdout dates are insufficient")
    if (
        len(value.first_embargo_dates) != 5
        or len(value.second_embargo_dates) != 5
        or len(value.terminal_embargo_dates) != 5
    ):
        raise ValueError("training split requires five-day embargoes")
    if len(value.early_stopping_dates) not in {0, 20} or len(value.calibration_dates) != 20:
        raise ValueError("training split requires supported early-stop and calibration dates")
    if len(value.fit_embargo_dates) != 5 or len(value.calibration_embargo_dates) not in {0, 5}:
        raise ValueError("training split requires internal label embargoes")
    if bool(value.early_stopping_dates) != bool(value.calibration_embargo_dates):
        raise ValueError("training split internal embargoes do not match early stopping")
    if (
        value.model_fit_dates
        + value.fit_embargo_dates
        + value.early_stopping_dates
        + value.calibration_embargo_dates
        + value.calibration_dates
        != value.development_dates
    ):
        raise ValueError("training development sub-splits are invalid")


def BuildTrainingDateSplit(
    dates: tuple[date, ...],
    *,
    parent_manifest_hash: str,
    fit_dates: int | None = None,
    early_stopping_sessions: int = 20,
    history_sessions: int = 61,
    recent_fit: bool = False,
    history_excludes_signal: bool = False,
) -> TrainingDateSplit:
    ordered = tuple(dates)
    if (
        early_stopping_sessions not in {0, 20}
        or (fit_dates is not None and fit_dates < 1)
        or history_sessions < 1
        or type(history_excludes_signal) is not bool
    ):
        raise ValueError("training fit window is invalid")
    warmup_sessions = history_sessions if history_excludes_signal else history_sessions - 1
    minimum_dates = max(
        MIN_TRAINING_DATES,
        warmup_sessions + 600 + 200 * 2 + 5 * 3 + TRAINING_POINT_IN_TIME_RESERVE,
    )
    if ordered != tuple(sorted(set(ordered))) or not minimum_dates <= len(ordered) <= MAX_TRAINING_SESSIONS:
        raise ValueError("training split requires enough unique dates for warmup and isolated holdouts")
    point_in_time = ordered[-TRAINING_POINT_IN_TIME_RESERVE:]
    cursor = len(ordered) - TRAINING_POINT_IN_TIME_RESERVE

    def take(count: int) -> tuple[date, ...]:
        nonlocal cursor
        if count < 0 or cursor < count:
            raise ValueError("training date budget is insufficient")
        cursor -= count
        return ordered[cursor : cursor + count]

    terminal_embargo = take(5)
    daily_proxy = take(200)
    second_embargo = take(5)
    confirmation = take(200)
    first_embargo = take(5)
    calibration = take(20)
    calibration_embargo = take(5) if early_stopping_sessions else ()
    early_stopping = take(early_stopping_sessions)
    fit_embargo = take(5)
    available_fit_days = cursor - warmup_sessions
    if available_fit_days < 1 or (fit_dates is not None and available_fit_days < fit_dates):
        raise ValueError("training fit dates are insufficient")
    model_fit = take(fit_dates if fit_dates is not None else available_fit_days)
    warmup = take(warmup_sessions)
    development = model_fit + fit_embargo + early_stopping + calibration_embargo + calibration
    return TrainingDateSplit(
        schema_version="v5_recent_training_split" if recent_fit else "training_date_split",
        parent_manifest_hash=parent_manifest_hash,
        label_contract=TrainingLabelContract(),
        model_fit_dates=model_fit,
        warmup_dates=warmup,
        fit_embargo_dates=fit_embargo,
        early_stopping_dates=early_stopping,
        calibration_embargo_dates=calibration_embargo,
        calibration_dates=calibration,
        development_dates=development,
        first_embargo_dates=first_embargo,
        confirmation_dates=confirmation,
        second_embargo_dates=second_embargo,
        daily_proxy_holdout_dates=daily_proxy,
        terminal_embargo_dates=terminal_embargo,
        point_in_time_holdout_dates=point_in_time,
    )


__all__ = ["TrainingLabelContract", "TrainingDateSplit", "BuildTrainingDateSplit"]

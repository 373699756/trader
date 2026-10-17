"""Immutable BaoStock daily-core contracts for offline research."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Literal, Protocol

from trader.infra.serialization.canonical_codec import CanonicalDigest

BAOSTOCK_RESEARCH_IDENTITY = "baostock_daily_core"
BAOSTOCK_SOURCE_CUTOFF = date(2026, 8, 31)
BAOSTOCK_MAX_SESSIONS = 2000
BAOSTOCK_POINT_IN_TIME_RESERVE = 200
BAOSTOCK_MIN_TRAINING_DATES = 1250

BaoStockBoard = Literal["main", "chinext", "star"]
BaoStockAdjustment = Literal["unadjusted", "qfq"]
BaoStockTradingStatus = Literal["trading", "suspended"]
BaoStockCellStatus = Literal[
    "complete",
    "supplier_marked_suspended",
    "tencent_inferred_suspended",
    "unadjusted_missing",
    "qfq_missing",
    "unknown_missing",
]
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CODE = re.compile(r"^[0-9]{6}$")
_BOARDS: tuple[BaoStockBoard, ...] = ("main", "chinext", "star")

BAOSTOCK_CALENDAR_SCHEMA = "baostock_exchange_calendar"
BAOSTOCK_DAILY_FACT_SCHEMA = "baostock_daily_fact"
BAOSTOCK_INDUSTRY_INTERVAL_SCHEMA = "baostock_industry_interval"
BAOSTOCK_CODE_DOWNLOAD_SCHEMA = "baostock_code_download"


class DailyTradingState(Protocol):
    @property
    def trading_status(self) -> BaoStockTradingStatus: ...


def ValidateDailySideValues(
    adjustment: BaoStockAdjustment,
    trading_status: BaoStockTradingStatus,
    prices: tuple[float | None, ...],
    flows: tuple[float | None, ...],
    raw_values: tuple[float | None, float | None, float | None],
) -> None:
    """Shared numeric qualification for supplier and published daily facts."""
    ValidateDailyPrices(adjustment, trading_status, prices, flows)
    ValidateDailyRawFields(adjustment, trading_status, raw_values, require_raw_fields=False)


def ValidateDailyPrices(
    adjustment: BaoStockAdjustment,
    trading_status: BaoStockTradingStatus,
    prices: tuple[float | None, ...],
    flows: tuple[float | None, ...],
) -> None:
    if adjustment not in ("unadjusted", "qfq") or trading_status not in ("trading", "suspended"):
        raise ValueError("BaoStock daily side semantics are invalid")
    supplied = tuple(value for value in (*prices, *flows) if value is not None)
    if any(not math.isfinite(value) or value < 0 for value in supplied):
        raise ValueError("BaoStock daily side contains an invalid number")
    if trading_status == "trading" and (
        any(value is None or value <= 0 for value in prices) or any(value is None or value < 0 for value in flows)
    ):
        raise ValueError("BaoStock active daily side requires complete OHLCV and amount")


def ValidateDailyRawFields(
    adjustment: BaoStockAdjustment,
    trading_status: BaoStockTradingStatus,
    raw_values: tuple[float | None, float | None, float | None],
    *,
    require_raw_fields: bool = True,
) -> None:
    if adjustment == "unadjusted":
        if (
            require_raw_fields
            and trading_status == "trading"
            and any(value is None or not math.isfinite(value) for value in raw_values)
        ):
            raise ValueError("BaoStock unadjusted side requires preclose, pct_change, and turnover")
        if any(value is not None and not math.isfinite(value) for value in raw_values):
            raise ValueError("BaoStock unadjusted side contains an invalid number")
        preclose, _, turnover = raw_values
        if (preclose is not None and preclose < 0) or (turnover is not None and turnover < 0):
            raise ValueError("BaoStock unadjusted side contains an invalid non-negative field")
    elif any(value is not None for value in raw_values):
        raise ValueError("BaoStock qfq side cannot carry unadjusted-only fields")


@dataclass(frozen=True)
class BaoStockDailySpec:
    sessions: int = BAOSTOCK_MAX_SESSIONS
    research_identity: str = BAOSTOCK_RESEARCH_IDENTITY
    source_cutoff: date = BAOSTOCK_SOURCE_CUTOFF
    production_authority: bool = False
    point_in_time_parity: bool = False
    schema_version: str = "baostock_daily_core"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if isinstance(self.sessions, bool) or not 1 <= self.sessions <= BAOSTOCK_MAX_SESSIONS:
            raise ValueError("BaoStock sessions must be in [1, 2000]")
        if type(self.source_cutoff) is not date:
            raise ValueError("BaoStock source cutoff must be a date")
        if self.research_identity != BAOSTOCK_RESEARCH_IDENTITY or self.schema_version != "baostock_daily_core":
            raise ValueError("BaoStock daily identity is invalid")
        if self.production_authority or self.point_in_time_parity:
            raise ValueError("BaoStock daily data cannot authorize production or point-in-time parity")
        object.__setattr__(self, "content_hash", CanonicalDigest(self))

    @property
    def authoritative(self) -> bool:
        return self.sessions == BAOSTOCK_MAX_SESSIONS


@dataclass(frozen=True)
class BaoStockSecurity:
    code: str
    name: str
    board: BaoStockBoard
    listed_on: date
    delisted_on: date | None
    source_version: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if _CODE.fullmatch(self.code) is None or not self.name.strip() or self.board not in _BOARDS:
            raise ValueError("BaoStock security identity is invalid")
        if self.delisted_on is not None and self.delisted_on <= self.listed_on:
            raise ValueError("BaoStock security delisting date is invalid")
        if not self.source_version.strip():
            raise ValueError("BaoStock security source version is required")
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "source_version", self.source_version.strip())
        object.__setattr__(self, "content_hash", CanonicalDigest(self))

    @property
    def source_code(self) -> str:
        exchange = "sh" if self.board == "star" or self.code.startswith("6") else "sz"
        return f"{exchange}.{self.code}"


@dataclass(frozen=True)
class BaoStockCalendar:
    open_dates: tuple[date, ...]
    schema_version: str = BAOSTOCK_CALENDAR_SCHEMA
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        values = tuple(self.open_dates)
        if not values or values != tuple(sorted(set(values))) or len(values) > BAOSTOCK_MAX_SESSIONS:
            raise ValueError("BaoStock open calendar must be non-empty, unique, ordered, and bounded")
        if self.schema_version != BAOSTOCK_CALENDAR_SCHEMA:
            raise ValueError("BaoStock calendar schema is invalid")
        object.__setattr__(self, "open_dates", values)
        object.__setattr__(self, "content_hash", CanonicalDigest(self))

    def expected_dates(self, security: BaoStockSecurity) -> tuple[date, ...]:
        return tuple(
            day
            for day in self.open_dates
            if day >= security.listed_on and (security.delisted_on is None or day < security.delisted_on)
        )


@dataclass(frozen=True)
class BaoStockDailySide:
    code: str
    trade_date: date
    adjustment: BaoStockAdjustment
    open_price: float | None
    high_price: float | None
    low_price: float | None
    close_price: float | None
    volume: float | None
    amount: float | None
    preclose: float | None
    pct_change: float | None
    turnover: float | None
    trading_status: BaoStockTradingStatus
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if _CODE.fullmatch(self.code) is None:
            raise ValueError("BaoStock daily side identity is invalid")
        ValidateDailySideValues(
            self.adjustment,
            self.trading_status,
            (self.open_price, self.high_price, self.low_price, self.close_price),
            (self.volume, self.amount),
            (self.preclose, self.pct_change, self.turnover),
        )
        object.__setattr__(self, "content_hash", CanonicalDigest(self))


@dataclass(frozen=True)
class BaoStockDailyCell:
    code: str
    trade_date: date
    status: BaoStockCellStatus
    unadjusted: BaoStockDailySide | None
    qfq: BaoStockDailySide | None
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if _CODE.fullmatch(self.code) is None:
            raise ValueError("BaoStock daily cell identity is invalid")
        for side, adjustment in ((self.unadjusted, "unadjusted"), (self.qfq, "qfq")):
            if side is not None and (
                side.code != self.code or side.trade_date != self.trade_date or side.adjustment != adjustment
            ):
                raise ValueError("BaoStock daily side does not match its logical cell")
        expected = (
            "tencent_inferred_suspended"
            if self.status == "tencent_inferred_suspended" and self.unadjusted is None and self.qfq is None
            else DailyCellStatus(self.unadjusted, self.qfq)
        )
        if self.status != expected:
            raise ValueError("BaoStock daily cell status does not match its sides")
        object.__setattr__(self, "content_hash", CanonicalDigest(self))

    @property
    def obtained(self) -> bool:
        return self.status in ("complete", "supplier_marked_suspended", "tencent_inferred_suspended")


@dataclass(frozen=True)
class BaoStockCodeBatch:
    code: str
    cells: tuple[BaoStockDailyCell, ...]
    duplicate_rows: int = 0
    null_rows: int = 0
    out_of_window_rows: int = 0
    future_rows: int = 0
    failure_reasons: tuple[str, ...] = ()
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        cells = tuple(sorted(self.cells, key=lambda item: item.trade_date))
        if _CODE.fullmatch(self.code) is None or any(item.code != self.code for item in cells):
            raise ValueError("BaoStock code batch identity is invalid")
        if len(cells) > BAOSTOCK_MAX_SESSIONS:
            raise ValueError("BaoStock code batch exceeds 2000 logical cells")
        if len({item.trade_date for item in cells}) != len(cells):
            raise ValueError("BaoStock code batch contains duplicate logical cells")
        counts = (self.duplicate_rows, self.null_rows, self.out_of_window_rows, self.future_rows)
        if any(isinstance(value, bool) or value < 0 for value in counts):
            raise ValueError("BaoStock code batch anomaly counts are invalid")
        reasons = tuple(sorted(set(self.failure_reasons)))
        if any(not reason or len(reason) > 64 for reason in reasons):
            raise ValueError("BaoStock code batch failure reason is invalid")
        object.__setattr__(self, "cells", cells)
        object.__setattr__(self, "failure_reasons", reasons)
        object.__setattr__(self, "content_hash", CanonicalDigest(self))


@dataclass(frozen=True)
class BaoStockDailyFact:
    code: str
    trade_date: date
    is_st: bool
    schema_version: str = BAOSTOCK_DAILY_FACT_SCHEMA
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if _CODE.fullmatch(self.code) is None:
            raise ValueError("BaoStock daily fact identity is invalid")
        if not isinstance(self.is_st, bool) or self.schema_version != BAOSTOCK_DAILY_FACT_SCHEMA:
            raise ValueError("BaoStock daily fact payload is invalid")
        object.__setattr__(self, "content_hash", CanonicalDigest(self))


@dataclass(frozen=True)
class BaoStockIndustryInterval:
    code: str
    effective_from: date
    effective_to: date | None
    industry: str
    classification: str
    schema_version: str = BAOSTOCK_INDUSTRY_INTERVAL_SCHEMA
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            _CODE.fullmatch(self.code) is None
            or (self.effective_to is not None and self.effective_to <= self.effective_from)
            or not self.industry.strip()
            or not self.classification.strip()
            or self.schema_version != BAOSTOCK_INDUSTRY_INTERVAL_SCHEMA
        ):
            raise ValueError("BaoStock industry interval is invalid")
        object.__setattr__(self, "industry", self.industry.strip())
        object.__setattr__(self, "classification", self.classification.strip())
        object.__setattr__(self, "content_hash", CanonicalDigest(self))


@dataclass(frozen=True)
class BaoStockCodeDownload:
    batch: BaoStockCodeBatch
    daily_facts: tuple[BaoStockDailyFact, ...]
    schema_version: str = BAOSTOCK_CODE_DOWNLOAD_SCHEMA
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        facts = tuple(sorted(self.daily_facts, key=lambda item: item.trade_date))
        if (
            any(item.code != self.batch.code for item in facts)
            or tuple(item.trade_date for item in facts) != tuple(item.trade_date for item in self.batch.cells)
            or self.schema_version != BAOSTOCK_CODE_DOWNLOAD_SCHEMA
        ):
            raise ValueError("BaoStock code download facts do not match its daily batch")
        object.__setattr__(self, "daily_facts", facts)
        object.__setattr__(self, "content_hash", CanonicalDigest(self))


@dataclass(frozen=True)
class BaoStockTrainingRow:
    code: str
    trade_date: date
    board: BaoStockBoard
    industry: str
    is_st: bool
    unadjusted: BaoStockDailySide
    qfq: BaoStockDailySide

    def __post_init__(self) -> None:
        if (
            _CODE.fullmatch(self.code) is None
            or self.board not in _BOARDS
            or not self.industry.strip()
            or self.unadjusted.code != self.code
            or self.qfq.code != self.code
            or self.unadjusted.trade_date != self.trade_date
            or self.qfq.trade_date != self.trade_date
        ):
            raise ValueError("BaoStock training row is invalid")


@dataclass(frozen=True)
class BaoStockDailyJoinRequest:
    code: str
    expected_dates: tuple[date, ...]
    source_cutoff: date
    null_rows: int = 0

    def __post_init__(self) -> None:
        expected = tuple(self.expected_dates)
        if _CODE.fullmatch(self.code) is None or type(self.source_cutoff) is not date:
            raise ValueError("BaoStock daily join identity is invalid")
        if expected != tuple(sorted(set(expected))):
            raise ValueError("BaoStock expected dates must be unique and ordered")
        if expected and expected[-1] > self.source_cutoff:
            raise ValueError("BaoStock expected dates exceed the active source cutoff")
        if isinstance(self.null_rows, bool) or self.null_rows < 0:
            raise ValueError("BaoStock null row count is invalid")
        object.__setattr__(self, "expected_dates", expected)


def JoinBaostockDailySides(
    request: BaoStockDailyJoinRequest,
    unadjusted: tuple[BaoStockDailySide, ...],
    qfq: tuple[BaoStockDailySide, ...],
) -> BaoStockCodeBatch:
    expected = request.expected_dates
    raw_by_date, raw_duplicates = _index_sides(request.code, "unadjusted", unadjusted)
    qfq_by_date, qfq_duplicates = _index_sides(request.code, "qfq", qfq)
    expected_set = set(expected)
    observed_dates = set(raw_by_date) | set(qfq_by_date)
    future = sum(day > request.source_cutoff for day in observed_dates)
    out_of_window = sum(day not in expected_set and day <= request.source_cutoff for day in observed_dates)
    cells = tuple(
        BaoStockDailyCell(
            request.code,
            day,
            DailyCellStatus(raw_by_date.get(day), qfq_by_date.get(day)),
            raw_by_date.get(day),
            qfq_by_date.get(day),
        )
        for day in expected
    )
    return BaoStockCodeBatch(
        request.code,
        cells,
        duplicate_rows=raw_duplicates + qfq_duplicates,
        null_rows=request.null_rows,
        out_of_window_rows=out_of_window,
        future_rows=future,
    )


def _index_sides(
    code: str,
    adjustment: BaoStockAdjustment,
    sides: tuple[BaoStockDailySide, ...],
) -> tuple[dict[date, BaoStockDailySide], int]:
    indexed: dict[date, BaoStockDailySide] = {}
    duplicates = 0
    for side in sides:
        if side.code != code or side.adjustment != adjustment:
            raise ValueError("BaoStock query side identity mismatch")
        previous = indexed.get(side.trade_date)
        if previous is not None:
            duplicates += 1
            if previous.content_hash != side.content_hash:
                raise ValueError("BaoStock query returned conflicting duplicate rows")
        else:
            indexed[side.trade_date] = side
    return indexed, duplicates


def DailyCellStatus(
    unadjusted: DailyTradingState | None,
    qfq: DailyTradingState | None,
) -> BaoStockCellStatus:
    if unadjusted is None and qfq is None:
        return "unknown_missing"
    if unadjusted is None:
        return "unadjusted_missing"
    if qfq is None:
        return "qfq_missing"
    if unadjusted.trading_status != qfq.trading_status:
        raise ValueError("BaoStock raw/qfq trading status mismatch")
    return "supplier_marked_suspended" if unadjusted.trading_status == "suspended" else "complete"


@dataclass(frozen=True)
class BaoStockSourceVersions:
    sdk_version: str
    python_version: str
    dependency_versions: tuple[tuple[str, str], ...]
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        dependencies = tuple(sorted(self.dependency_versions))
        if not self.sdk_version.strip() or not self.python_version.strip():
            raise ValueError("BaoStock source versions are required")
        if any(not name.strip() or not version.strip() for name, version in dependencies):
            raise ValueError("BaoStock dependency version is invalid")
        if len({name for name, _ in dependencies}) != len(dependencies):
            raise ValueError("BaoStock dependency versions must be unique")
        object.__setattr__(self, "dependency_versions", dependencies)
        object.__setattr__(self, "content_hash", CanonicalDigest(self))


@dataclass(frozen=True)
class BaoStockTrainingLabelContract:
    formula: str = "(close[D+1]/close[D]-1)-eligible_universe_equal_weight_return[D+1]"
    primary_cost_bps: int = 20
    gate_cost_bps: int = 50
    stress_cost_bps: int = 100
    label_pending_required: bool = True
    schema_version: str = "pre_cost_next_day_excess_label"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.formula != "(close[D+1]/close[D]-1)-eligible_universe_equal_weight_return[D+1]":
            raise ValueError("BaoStock training label formula is fixed")
        if (self.primary_cost_bps, self.gate_cost_bps, self.stress_cost_bps) != (20, 50, 100):
            raise ValueError("BaoStock training label costs are fixed")
        if not self.label_pending_required or self.schema_version != "pre_cost_next_day_excess_label":
            raise ValueError("BaoStock training label contract is invalid")
        object.__setattr__(self, "content_hash", CanonicalDigest(self))


@dataclass(frozen=True)
class BaoStockTrainingSplit:
    parent_manifest_hash: str
    label_contract: BaoStockTrainingLabelContract
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
    schema_version: str = "baostock_training_split"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if _SHA256.fullmatch(self.parent_manifest_hash) is None:
            raise ValueError("BaoStock training split parent hash is invalid")
        _validate_training_split_dates(self)
        if self.training_anchor != "15:00_daily_close" or self.point_in_time_parity:
            raise ValueError("BaoStock training split must remain a daily-close proxy")
        if self.terminal_holdout_opened or self.production_authority:
            raise ValueError("BaoStock training split cannot open holdouts or authorize production")
        if self.schema_version != "baostock_training_split":
            raise ValueError("BaoStock training split schema is invalid")
        object.__setattr__(self, "content_hash", CanonicalDigest(self))


def _validate_training_split_dates(value: BaoStockTrainingSplit) -> None:
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
        raise ValueError("BaoStock training split dates must be unique and chronological")
    if len(value.development_dates) < 600 or len(value.confirmation_dates) < 200:
        raise ValueError("BaoStock training development or confirmation dates are insufficient")
    if len(value.daily_proxy_holdout_dates) < 200 or len(value.point_in_time_holdout_dates) != 200:
        raise ValueError("BaoStock training holdout dates are insufficient")
    if (
        len(value.first_embargo_dates) != 5
        or len(value.second_embargo_dates) != 5
        or len(value.terminal_embargo_dates) != 5
    ):
        raise ValueError("BaoStock training split requires two five-day embargoes")
    if len(value.early_stopping_dates) not in {0, 20} or len(value.calibration_dates) != 20:
        raise ValueError("BaoStock training split requires supported early-stop and calibration dates")
    if len(value.fit_embargo_dates) != 5 or len(value.calibration_embargo_dates) not in {0, 5}:
        raise ValueError("BaoStock training split requires internal label embargoes")
    if bool(value.early_stopping_dates) != bool(value.calibration_embargo_dates):
        raise ValueError("BaoStock training split internal embargoes do not match early stopping")
    if (
        value.model_fit_dates
        + value.fit_embargo_dates
        + value.early_stopping_dates
        + value.calibration_embargo_dates
        + value.calibration_dates
        != value.development_dates
    ):
        raise ValueError("BaoStock training development sub-splits are invalid")


def BuildBaostockTrainingSplit(
    dates: tuple[date, ...],
    *,
    parent_manifest_hash: str,
    fit_dates: int | None = None,
    early_stopping_sessions: int = 20,
    history_sessions: int = 61,
) -> BaoStockTrainingSplit:
    ordered = tuple(dates)
    if early_stopping_sessions not in {0, 20} or (fit_dates is not None and fit_dates < 1) or history_sessions < 1:
        raise ValueError("BaoStock training fit window is invalid")
    minimum_dates = max(
        BAOSTOCK_MIN_TRAINING_DATES,
        history_sessions - 1 + 600 + 200 * 2 + 5 * 3 + BAOSTOCK_POINT_IN_TIME_RESERVE,
    )
    if ordered != tuple(sorted(set(ordered))) or not minimum_dates <= len(ordered) <= BAOSTOCK_MAX_SESSIONS:
        raise ValueError("BaoStock training split requires enough unique dates for warmup and isolated holdouts")
    point_in_time = ordered[-BAOSTOCK_POINT_IN_TIME_RESERVE:]
    cursor = len(ordered) - BAOSTOCK_POINT_IN_TIME_RESERVE

    def take(count: int) -> tuple[date, ...]:
        nonlocal cursor
        if count < 0 or cursor < count:
            raise ValueError("BaoStock training date budget is insufficient")
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
    available_fit_days = cursor - (history_sessions - 1)
    if available_fit_days < 1 or (fit_dates is not None and available_fit_days < fit_dates):
        raise ValueError("BaoStock training fit dates are insufficient")
    model_fit = take(fit_dates if fit_dates is not None else available_fit_days)
    warmup = take(history_sessions - 1)
    development = model_fit + fit_embargo + early_stopping + calibration_embargo + calibration
    return BaoStockTrainingSplit(
        parent_manifest_hash=parent_manifest_hash,
        label_contract=BaoStockTrainingLabelContract(),
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


__all__ = [
    "BAOSTOCK_CALENDAR_SCHEMA",
    "BAOSTOCK_CODE_DOWNLOAD_SCHEMA",
    "BAOSTOCK_DAILY_FACT_SCHEMA",
    "BAOSTOCK_INDUSTRY_INTERVAL_SCHEMA",
    "BAOSTOCK_MAX_SESSIONS",
    "BAOSTOCK_RESEARCH_IDENTITY",
    "BAOSTOCK_SOURCE_CUTOFF",
    "BaoStockAdjustment",
    "BaoStockBoard",
    "BaoStockCalendar",
    "BaoStockCellStatus",
    "BaoStockCodeBatch",
    "BaoStockCodeDownload",
    "BaoStockDailyCell",
    "BaoStockDailyFact",
    "BaoStockDailyJoinRequest",
    "BaoStockDailySide",
    "BaoStockDailySpec",
    "BaoStockIndustryInterval",
    "BaoStockSecurity",
    "BaoStockSourceVersions",
    "BaoStockTradingStatus",
    "BaoStockTrainingLabelContract",
    "BaoStockTrainingRow",
    "BaoStockTrainingSplit",
    "DailyTradingState",
    "BuildBaostockTrainingSplit",
    "DailyCellStatus",
    "JoinBaostockDailySides",
    "ValidateDailySideValues",
    "ValidateDailyPrices",
    "ValidateDailyRawFields",
]

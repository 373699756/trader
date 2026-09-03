"""Immutable BaoStock v2 daily-core contracts for offline research."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from trader.domain.research.h1_point_in_time import canonical_hash
from trader.domain.research.historical_effective_facts import HistoricalEffectiveFactsAudit

BAOSTOCK_RESEARCH_IDENTITY = "score_baostock_daily_core_v2"
BAOSTOCK_SOURCE_CUTOFF = date(2026, 8, 31)
BAOSTOCK_MAX_SESSIONS = 2000
BAOSTOCK_MIN_COVERAGE = 0.95
BAOSTOCK_FAILED_CODE_COVERAGE = 0.90
BAOSTOCK_POINT_IN_TIME_RESERVE = 200
BAOSTOCK_MIN_V3_DATES = 1250

BaoStockBoard = Literal["main", "chinext", "star"]
BaoStockAdjustment = Literal["unadjusted", "qfq"]
BaoStockTradingStatus = Literal["trading", "suspended"]
BaoStockCellStatus = Literal[
    "complete",
    "supplier_marked_suspended",
    "unadjusted_missing",
    "qfq_missing",
    "unknown_missing",
]
BaoStockCoverageStatus = Literal["coverage_ready", "historical_data_insufficient"]
BaoStockV3DatasetStatus = Literal["dataset_ready", "historical_data_insufficient"]

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CODE = re.compile(r"^[0-9]{6}$")
_BOARDS: tuple[BaoStockBoard, ...] = ("main", "chinext", "star")


@dataclass(frozen=True)
class BaoStockDailySpec:
    sessions: int = BAOSTOCK_MAX_SESSIONS
    research_identity: str = BAOSTOCK_RESEARCH_IDENTITY
    source_cutoff: date = BAOSTOCK_SOURCE_CUTOFF
    production_authority: bool = False
    point_in_time_parity: bool = False
    schema_version: str = "score_baostock_daily_core_v2"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if isinstance(self.sessions, bool) or not 1 <= self.sessions <= BAOSTOCK_MAX_SESSIONS:
            raise ValueError("BaoStock sessions must be in [1, 2000]")
        if self.research_identity != BAOSTOCK_RESEARCH_IDENTITY or self.source_cutoff != BAOSTOCK_SOURCE_CUTOFF:
            raise ValueError("BaoStock v2 identity and source cutoff are fixed")
        if self.production_authority or self.point_in_time_parity:
            raise ValueError("BaoStock daily data cannot authorize production or point-in-time parity")
        if self.schema_version != "score_baostock_daily_core_v2":
            raise ValueError("BaoStock v2 schema is invalid")
        object.__setattr__(self, "content_hash", canonical_hash(self))

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
        if self.listed_on > BAOSTOCK_SOURCE_CUTOFF:
            raise ValueError("BaoStock security listing date exceeds source cutoff")
        if self.delisted_on is not None and self.delisted_on <= self.listed_on:
            raise ValueError("BaoStock security delisting date is invalid")
        if not self.source_version.strip():
            raise ValueError("BaoStock security source version is required")
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "source_version", self.source_version.strip())
        object.__setattr__(self, "content_hash", canonical_hash(self))

    @property
    def source_code(self) -> str:
        exchange = "sh" if self.board == "star" or self.code.startswith("6") else "sz"
        return f"{exchange}.{self.code}"


@dataclass(frozen=True)
class BaoStockCalendar:
    open_dates: tuple[date, ...]
    schema_version: str = "baostock_exchange_calendar_v2"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        values = tuple(self.open_dates)
        if not values or values != tuple(sorted(set(values))) or len(values) > BAOSTOCK_MAX_SESSIONS:
            raise ValueError("BaoStock open calendar must be non-empty, unique, ordered, and bounded")
        if values[-1] > BAOSTOCK_SOURCE_CUTOFF:
            raise ValueError("BaoStock calendar exceeds source cutoff")
        if self.schema_version != "baostock_exchange_calendar_v2":
            raise ValueError("BaoStock calendar schema is invalid")
        object.__setattr__(self, "open_dates", values)
        object.__setattr__(self, "content_hash", canonical_hash(self))

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
        if _CODE.fullmatch(self.code) is None or self.trade_date > BAOSTOCK_SOURCE_CUTOFF:
            raise ValueError("BaoStock daily side identity is invalid")
        if self.adjustment not in ("unadjusted", "qfq") or self.trading_status not in ("trading", "suspended"):
            raise ValueError("BaoStock daily side semantics are invalid")
        prices = (self.open_price, self.high_price, self.low_price, self.close_price)
        flows = (self.volume, self.amount)
        supplied = tuple(value for value in (*prices, *flows) if value is not None)
        if any(not math.isfinite(value) or value < 0 for value in supplied):
            raise ValueError("BaoStock daily side contains an invalid number")
        if self.trading_status == "trading" and (
            any(value is None or value <= 0 for value in prices) or any(value is None or value < 0 for value in flows)
        ):
            raise ValueError("BaoStock active daily side requires complete OHLCV and amount")
        if self.adjustment == "unadjusted":
            raw_values = (self.preclose, self.pct_change, self.turnover)
            if self.trading_status == "trading" and any(
                value is None or not math.isfinite(value) for value in raw_values
            ):
                raise ValueError("BaoStock unadjusted side requires preclose, pct_change, and turnover")
            if any(value is not None and not math.isfinite(value) for value in raw_values):
                raise ValueError("BaoStock unadjusted side contains an invalid number")
            if (self.preclose is not None and self.preclose < 0) or (self.turnover is not None and self.turnover < 0):
                raise ValueError("BaoStock unadjusted side contains an invalid non-negative field")
        elif any(value is not None for value in (self.preclose, self.pct_change, self.turnover)):
            raise ValueError("BaoStock qfq side cannot carry unadjusted-only fields")
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class BaoStockDailyCell:
    code: str
    trade_date: date
    status: BaoStockCellStatus
    unadjusted: BaoStockDailySide | None
    qfq: BaoStockDailySide | None
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if _CODE.fullmatch(self.code) is None or self.trade_date > BAOSTOCK_SOURCE_CUTOFF:
            raise ValueError("BaoStock daily cell identity is invalid")
        for side, adjustment in ((self.unadjusted, "unadjusted"), (self.qfq, "qfq")):
            if side is not None and (
                side.code != self.code or side.trade_date != self.trade_date or side.adjustment != adjustment
            ):
                raise ValueError("BaoStock daily side does not match its logical cell")
        expected = _cell_status(self.unadjusted, self.qfq)
        if self.status != expected:
            raise ValueError("BaoStock daily cell status does not match its sides")
        object.__setattr__(self, "content_hash", canonical_hash(self))

    @property
    def obtained(self) -> bool:
        return self.status in ("complete", "supplier_marked_suspended")


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
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class BaoStockDailyFact:
    code: str
    trade_date: date
    is_st: bool
    schema_version: str = "baostock_daily_fact_v1"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if _CODE.fullmatch(self.code) is None or self.trade_date > BAOSTOCK_SOURCE_CUTOFF:
            raise ValueError("BaoStock daily fact identity is invalid")
        if not isinstance(self.is_st, bool) or self.schema_version != "baostock_daily_fact_v1":
            raise ValueError("BaoStock daily fact payload is invalid")
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class BaoStockIndustryInterval:
    code: str
    effective_from: date
    effective_to: date | None
    industry: str
    classification: str
    schema_version: str = "baostock_industry_interval_v1"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            _CODE.fullmatch(self.code) is None
            or self.effective_from > BAOSTOCK_SOURCE_CUTOFF
            or (self.effective_to is not None and self.effective_to <= self.effective_from)
            or not self.industry.strip()
            or not self.classification.strip()
            or self.schema_version != "baostock_industry_interval_v1"
        ):
            raise ValueError("BaoStock industry interval is invalid")
        object.__setattr__(self, "industry", self.industry.strip())
        object.__setattr__(self, "classification", self.classification.strip())
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class BaoStockCodeDownload:
    batch: BaoStockCodeBatch
    daily_facts: tuple[BaoStockDailyFact, ...]
    schema_version: str = "baostock_code_download_v1"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        facts = tuple(sorted(self.daily_facts, key=lambda item: item.trade_date))
        if (
            any(item.code != self.batch.code for item in facts)
            or tuple(item.trade_date for item in facts) != tuple(item.trade_date for item in self.batch.cells)
            or self.schema_version != "baostock_code_download_v1"
        ):
            raise ValueError("BaoStock code download facts do not match its daily batch")
        object.__setattr__(self, "daily_facts", facts)
        object.__setattr__(self, "content_hash", canonical_hash(self))


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


def join_baostock_daily_sides(
    code: str,
    expected_dates: tuple[date, ...],
    unadjusted: tuple[BaoStockDailySide, ...],
    qfq: tuple[BaoStockDailySide, ...],
    *,
    null_rows: int = 0,
) -> BaoStockCodeBatch:
    expected = tuple(expected_dates)
    if expected != tuple(sorted(set(expected))):
        raise ValueError("BaoStock expected dates must be unique and ordered")
    raw_by_date, raw_duplicates = _index_sides(code, "unadjusted", unadjusted)
    qfq_by_date, qfq_duplicates = _index_sides(code, "qfq", qfq)
    expected_set = set(expected)
    observed_dates = set(raw_by_date) | set(qfq_by_date)
    future = sum(day > BAOSTOCK_SOURCE_CUTOFF for day in observed_dates)
    out_of_window = sum(day not in expected_set and day <= BAOSTOCK_SOURCE_CUTOFF for day in observed_dates)
    cells = tuple(
        BaoStockDailyCell(
            code,
            day,
            _cell_status(raw_by_date.get(day), qfq_by_date.get(day)),
            raw_by_date.get(day),
            qfq_by_date.get(day),
        )
        for day in expected
    )
    return BaoStockCodeBatch(
        code,
        cells,
        duplicate_rows=raw_duplicates + qfq_duplicates,
        null_rows=null_rows,
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


def _cell_status(
    unadjusted: BaoStockDailySide | None,
    qfq: BaoStockDailySide | None,
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
class BaoStockBoardCoverage:
    board: BaoStockBoard
    expected_cells: int
    obtained_cells: int
    coverage_ratio: float

    def __post_init__(self) -> None:
        if self.board not in _BOARDS or min(self.expected_cells, self.obtained_cells) < 0:
            raise ValueError("BaoStock board coverage identity is invalid")
        if self.obtained_cells > self.expected_cells or not 0 <= self.coverage_ratio <= 1:
            raise ValueError("BaoStock board coverage values are invalid")
        expected_ratio = self.obtained_cells / self.expected_cells if self.expected_cells else 0.0
        if not math.isclose(self.coverage_ratio, expected_ratio):
            raise ValueError("BaoStock board coverage ratio does not match counts")


@dataclass(frozen=True)
class BaoStockCodeCoverage:
    code: str
    expected_cells: int
    obtained_cells: int
    coverage_ratio: float
    eligible_for_v3_population: bool

    def __post_init__(self) -> None:
        if _CODE.fullmatch(self.code) is None or min(self.expected_cells, self.obtained_cells) < 0:
            raise ValueError("BaoStock code coverage identity is invalid")
        if self.obtained_cells > self.expected_cells or not 0 <= self.coverage_ratio <= 1:
            raise ValueError("BaoStock code coverage values are invalid")
        expected_ratio = self.obtained_cells / self.expected_cells if self.expected_cells else 1.0
        if not math.isclose(self.coverage_ratio, expected_ratio):
            raise ValueError("BaoStock code coverage ratio does not match counts")
        eligible = self.expected_cells > 0 and self.coverage_ratio >= BAOSTOCK_FAILED_CODE_COVERAGE
        if self.eligible_for_v3_population != eligible:
            raise ValueError("BaoStock code V3 eligibility does not match coverage")


@dataclass(frozen=True)
class BaoStockCoverageAudit:
    spec_hash: str
    calendar_hash: str
    universe_hash: str
    calendar_sessions: int
    calendar_first_date: date
    calendar_last_date: date
    universe_count: int
    expected_cells: int
    obtained_cells: int
    all_cell_coverage: float
    board_coverages: tuple[BaoStockBoardCoverage, ...]
    code_coverages: tuple[BaoStockCodeCoverage, ...]
    full_window_stock_count: int
    full_window_stocks_at_95_percent: int
    full_window_stock_success_ratio: float
    failed_codes: tuple[str, ...]
    duplicate_rows: int
    null_rows: int
    out_of_window_rows: int
    future_rows: int
    latest_reserved_dates: tuple[date, ...]
    status: BaoStockCoverageStatus
    failure_reasons: tuple[str, ...]
    terminal_holdout_opened: bool = False
    production_authority: bool = False
    point_in_time_parity: bool = False
    schema_version: str = "baostock_daily_coverage_audit_v2"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _validate_coverage_identity(self)
        _validate_coverage_counts(self)
        _validate_coverage_ratios(self)
        reasons = tuple(sorted(set(self.failure_reasons)))
        if (self.status == "coverage_ready") == bool(reasons):
            raise ValueError("BaoStock coverage status and reasons are inconsistent")
        if self.terminal_holdout_opened or self.production_authority or self.point_in_time_parity:
            raise ValueError("BaoStock coverage cannot open holdout or authorize production/parity")
        object.__setattr__(self, "failed_codes", tuple(sorted(set(self.failed_codes))))
        object.__setattr__(self, "failure_reasons", reasons)
        object.__setattr__(self, "content_hash", canonical_hash(self))


def _validate_coverage_identity(value: BaoStockCoverageAudit) -> None:
    if any(_SHA256.fullmatch(item) is None for item in (value.spec_hash, value.calendar_hash, value.universe_hash)):
        raise ValueError("BaoStock coverage parent hash is invalid")
    if tuple(item.board for item in value.board_coverages) != _BOARDS:
        raise ValueError("BaoStock board coverage order is invalid")
    codes = tuple(item.code for item in value.code_coverages)
    if codes != tuple(sorted(codes)) or value.universe_count != len(codes):
        raise ValueError("BaoStock code coverage identity is inconsistent")
    if value.calendar_sessions <= 0 or value.calendar_first_date > value.calendar_last_date:
        raise ValueError("BaoStock coverage calendar range is invalid")
    if value.status not in ("coverage_ready", "historical_data_insufficient"):
        raise ValueError("BaoStock coverage status is invalid")


def _validate_coverage_counts(value: BaoStockCoverageAudit) -> None:
    counts = (
        value.calendar_sessions,
        value.universe_count,
        value.expected_cells,
        value.obtained_cells,
        value.full_window_stock_count,
        value.full_window_stocks_at_95_percent,
        value.duplicate_rows,
        value.null_rows,
        value.out_of_window_rows,
        value.future_rows,
    )
    if any(item < 0 for item in counts) or value.obtained_cells > value.expected_cells:
        raise ValueError("BaoStock coverage counts are invalid")
    expected = sum(item.expected_cells for item in value.board_coverages)
    obtained = sum(item.obtained_cells for item in value.board_coverages)
    code_expected = sum(item.expected_cells for item in value.code_coverages)
    code_obtained = sum(item.obtained_cells for item in value.code_coverages)
    if (expected, obtained, code_expected, code_obtained) != (
        value.expected_cells,
        value.obtained_cells,
        value.expected_cells,
        value.obtained_cells,
    ):
        raise ValueError("BaoStock coverage aggregate counts are inconsistent")
    if not 0 <= value.full_window_stocks_at_95_percent <= value.full_window_stock_count <= value.universe_count:
        raise ValueError("BaoStock full-window coverage counts are inconsistent")


def _validate_coverage_ratios(value: BaoStockCoverageAudit) -> None:
    rates = (
        value.all_cell_coverage,
        value.full_window_stock_success_ratio,
        *(item.coverage_ratio for item in value.board_coverages),
    )
    if any(not math.isfinite(item) or not 0 <= item <= 1 for item in rates):
        raise ValueError("BaoStock coverage ratio is invalid")
    expected_ratio = value.obtained_cells / value.expected_cells if value.expected_cells else 0.0
    full_ratio = (
        value.full_window_stocks_at_95_percent / value.full_window_stock_count if value.full_window_stock_count else 0.0
    )
    if not math.isclose(value.all_cell_coverage, expected_ratio) or not math.isclose(
        value.full_window_stock_success_ratio, full_ratio
    ):
        raise ValueError("BaoStock coverage aggregate ratios are inconsistent")
    expected_failed = tuple(
        item.code
        for item in value.code_coverages
        if item.expected_cells and item.coverage_ratio < BAOSTOCK_FAILED_CODE_COVERAGE
    )
    if tuple(sorted(set(value.failed_codes))) != expected_failed:
        raise ValueError("BaoStock failed code coverage is inconsistent")


@dataclass(frozen=True)
class _CoverageSummary:
    board_expected: tuple[tuple[BaoStockBoard, int], ...]
    board_obtained: tuple[tuple[BaoStockBoard, int], ...]
    code_coverages: tuple[BaoStockCodeCoverage, ...]
    expected_total: int
    obtained_total: int
    full_window: int
    full_window_success: int
    failed_codes: tuple[str, ...]


def _summarize_coverage(
    calendar: BaoStockCalendar,
    securities: tuple[BaoStockSecurity, ...],
    batches: tuple[BaoStockCodeBatch, ...],
) -> _CoverageSummary:
    universe_codes = {item.code for item in securities}
    by_code = {item.code: item for item in batches}
    if len(universe_codes) != len(securities):
        raise ValueError("BaoStock coverage universe contains duplicate codes")
    if len(by_code) != len(batches) or not set(by_code) <= universe_codes:
        raise ValueError("BaoStock coverage batches do not match the universe")
    expected_by_board: dict[BaoStockBoard, int] = {board: 0 for board in _BOARDS}
    obtained_by_board: dict[BaoStockBoard, int] = {board: 0 for board in _BOARDS}
    expected_total = obtained_total = 0
    full_window = full_window_success = 0
    failed_codes: list[str] = []
    code_coverages: list[BaoStockCodeCoverage] = []
    for security in securities:
        expected_dates = calendar.expected_dates(security)
        batch = by_code.get(security.code)
        cells = {item.trade_date: item for item in batch.cells} if batch is not None else {}
        expected_count = len(expected_dates)
        obtained_count = sum(cells.get(day) is not None and cells[day].obtained for day in expected_dates)
        expected_total += expected_count
        obtained_total += obtained_count
        expected_by_board[security.board] += expected_count
        obtained_by_board[security.board] += obtained_count
        ratio = obtained_count / expected_count if expected_count else 1.0
        if expected_count == len(calendar.open_dates):
            full_window += 1
            full_window_success += ratio >= BAOSTOCK_MIN_COVERAGE
        if expected_count and ratio < BAOSTOCK_FAILED_CODE_COVERAGE:
            failed_codes.append(security.code)
        code_coverages.append(
            BaoStockCodeCoverage(
                security.code,
                expected_count,
                obtained_count,
                ratio,
                expected_count > 0 and ratio >= BAOSTOCK_FAILED_CODE_COVERAGE,
            )
        )
    return _CoverageSummary(
        tuple((board, expected_by_board[board]) for board in _BOARDS),
        tuple((board, obtained_by_board[board]) for board in _BOARDS),
        tuple(code_coverages),
        expected_total,
        obtained_total,
        full_window,
        full_window_success,
        tuple(failed_codes),
    )


def _coverage_scope_reasons(
    spec: BaoStockDailySpec,
    calendar: BaoStockCalendar,
    overall: float,
    board_rates: tuple[BaoStockBoardCoverage, ...],
) -> tuple[str, ...]:
    reasons: list[str] = []
    if not spec.authoritative or len(calendar.open_dates) != BAOSTOCK_MAX_SESSIONS:
        reasons.append("authoritative_calendar_below_2000")
    if len(calendar.open_dates) != spec.sessions:
        reasons.append("calendar_session_count_mismatch")
    if overall < BAOSTOCK_MIN_COVERAGE:
        reasons.append("all_expected_cell_coverage_below_95_percent")
    if any(item.expected_cells and item.coverage_ratio < BAOSTOCK_MIN_COVERAGE for item in board_rates):
        reasons.append("board_expected_cell_coverage_below_95_percent")
    if len(calendar.open_dates) < BAOSTOCK_POINT_IN_TIME_RESERVE:
        reasons.append("point_in_time_reserve_below_200")
    return tuple(reasons)


def _coverage_integrity_reasons(
    summary: _CoverageSummary,
    batches: tuple[BaoStockCodeBatch, ...],
) -> tuple[str, ...]:
    reasons: list[str] = []
    old_stock_ratio = summary.full_window_success / summary.full_window if summary.full_window else 0.0
    if not summary.full_window:
        reasons.append("full_window_stock_population_missing")
    elif old_stock_ratio < BAOSTOCK_MIN_COVERAGE:
        reasons.append("full_window_stock_completeness_below_95_percent")
    anomalies = (
        (sum(item.duplicate_rows for item in batches), "duplicate_rows_present"),
        (sum(item.null_rows for item in batches), "null_rows_present"),
        (sum(item.out_of_window_rows for item in batches), "out_of_window_rows_present"),
        (sum(item.future_rows for item in batches), "future_rows_present"),
    )
    reasons.extend(reason for count, reason in anomalies if count)
    if any(item.failure_reasons for item in batches):
        reasons.append("code_batch_failures_present")
    return tuple(reasons)


def build_baostock_coverage_audit(
    spec: BaoStockDailySpec,
    calendar: BaoStockCalendar,
    universe: tuple[BaoStockSecurity, ...],
    batches: tuple[BaoStockCodeBatch, ...],
) -> BaoStockCoverageAudit:
    securities = tuple(sorted(universe, key=lambda item: item.code))
    summary = _summarize_coverage(calendar, securities, batches)
    expected_by_board = dict(summary.board_expected)
    obtained_by_board = dict(summary.board_obtained)
    board_rates = tuple(
        BaoStockBoardCoverage(
            board,
            expected_by_board[board],
            obtained_by_board[board],
            obtained_by_board[board] / expected_by_board[board] if expected_by_board[board] else 0.0,
        )
        for board in _BOARDS
    )
    overall = summary.obtained_total / summary.expected_total if summary.expected_total else 0.0
    old_stock_ratio = summary.full_window_success / summary.full_window if summary.full_window else 0.0
    duplicate_rows = sum(item.duplicate_rows for item in batches)
    null_rows = sum(item.null_rows for item in batches)
    out_of_window_rows = sum(item.out_of_window_rows for item in batches)
    future_rows = sum(item.future_rows for item in batches)
    reasons = _coverage_scope_reasons(spec, calendar, overall, board_rates) + _coverage_integrity_reasons(
        summary, batches
    )
    return BaoStockCoverageAudit(
        spec_hash=spec.content_hash,
        calendar_hash=calendar.content_hash,
        universe_hash=canonical_hash(securities),
        calendar_sessions=len(calendar.open_dates),
        calendar_first_date=calendar.open_dates[0],
        calendar_last_date=calendar.open_dates[-1],
        universe_count=len(securities),
        expected_cells=summary.expected_total,
        obtained_cells=summary.obtained_total,
        all_cell_coverage=overall,
        board_coverages=board_rates,
        code_coverages=summary.code_coverages,
        full_window_stock_count=summary.full_window,
        full_window_stocks_at_95_percent=summary.full_window_success,
        full_window_stock_success_ratio=old_stock_ratio,
        failed_codes=summary.failed_codes,
        duplicate_rows=duplicate_rows,
        null_rows=null_rows,
        out_of_window_rows=out_of_window_rows,
        future_rows=future_rows,
        latest_reserved_dates=calendar.open_dates[-BAOSTOCK_POINT_IN_TIME_RESERVE:],
        status="coverage_ready" if not reasons else "historical_data_insufficient",
        failure_reasons=tuple(reasons),
    )


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
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class BaoStockPartitionRef:
    relative_path: str
    board: BaoStockBoard
    code_prefix: str
    codes: tuple[str, ...]
    row_count: int
    logical_records_hash: str
    database_sha256: str
    schema_version: str = "baostock_partition_ref_v1"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        codes = tuple(sorted(self.codes))
        expected_name = f"shards/{self.board}-{self.code_prefix}.sqlite3"
        if (
            self.board not in _BOARDS
            or not re.fullmatch(r"[0-9]{4}", self.code_prefix)
            or self.relative_path != expected_name
            or not codes
            or len(codes) > 100
            or any(_CODE.fullmatch(code) is None or not code.startswith(self.code_prefix) for code in codes)
            or len(set(codes)) != len(codes)
            or self.row_count < 0
            or _SHA256.fullmatch(self.logical_records_hash) is None
            or _SHA256.fullmatch(self.database_sha256) is None
            or self.schema_version != "baostock_partition_ref_v1"
        ):
            raise ValueError("BaoStock partition reference is invalid")
        object.__setattr__(self, "codes", codes)
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class BaoStockV3LabelContract:
    formula: str = "(close[D+1]/close[D]-1)-eligible_universe_equal_weight_return[D+1]-round_trip_cost"
    primary_cost_bps: int = 20
    gate_cost_bps: int = 50
    stress_cost_bps: int = 100
    label_pending_required: bool = True
    schema_version: str = "tomorrow_v3_daily_label_v1"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.formula != ("(close[D+1]/close[D]-1)-eligible_universe_equal_weight_return[D+1]-round_trip_cost"):
            raise ValueError("BaoStock V3 label formula is fixed")
        if (self.primary_cost_bps, self.gate_cost_bps, self.stress_cost_bps) != (20, 50, 100):
            raise ValueError("BaoStock V3 label costs are fixed")
        if not self.label_pending_required or self.schema_version != "tomorrow_v3_daily_label_v1":
            raise ValueError("BaoStock V3 label contract is invalid")
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class BaoStockV3Split:
    parent_manifest_hash: str
    label_contract: BaoStockV3LabelContract
    model_fit_dates: tuple[date, ...]
    early_stopping_dates: tuple[date, ...]
    calibration_dates: tuple[date, ...]
    development_dates: tuple[date, ...]
    first_embargo_dates: tuple[date, ...]
    confirmation_dates: tuple[date, ...]
    second_embargo_dates: tuple[date, ...]
    daily_proxy_holdout_dates: tuple[date, ...]
    point_in_time_holdout_dates: tuple[date, ...]
    training_anchor: str = "15:00_daily_close"
    point_in_time_parity: bool = False
    terminal_holdout_opened: bool = False
    production_authority: bool = False
    schema_version: str = "tomorrow_v3_baostock_split_v1"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if _SHA256.fullmatch(self.parent_manifest_hash) is None:
            raise ValueError("BaoStock V3 split parent hash is invalid")
        _validate_v3_split_dates(self)
        if self.training_anchor != "15:00_daily_close" or self.point_in_time_parity:
            raise ValueError("BaoStock V3 split must remain a daily-close proxy")
        if self.terminal_holdout_opened or self.production_authority:
            raise ValueError("BaoStock V3 split cannot open holdouts or authorize production")
        if self.schema_version != "tomorrow_v3_baostock_split_v1":
            raise ValueError("BaoStock V3 split schema is invalid")
        object.__setattr__(self, "content_hash", canonical_hash(self))


def _validate_v3_split_dates(value: BaoStockV3Split) -> None:
    ordered_groups = (
        value.development_dates,
        value.first_embargo_dates,
        value.confirmation_dates,
        value.second_embargo_dates,
        value.daily_proxy_holdout_dates,
        value.point_in_time_holdout_dates,
    )
    flattened = tuple(day for group in ordered_groups for day in group)
    if flattened != tuple(sorted(set(flattened))):
        raise ValueError("BaoStock V3 split dates must be unique and chronological")
    if len(value.development_dates) < 600 or len(value.confirmation_dates) < 200:
        raise ValueError("BaoStock V3 development or confirmation dates are insufficient")
    if len(value.daily_proxy_holdout_dates) < 200 or len(value.point_in_time_holdout_dates) != 200:
        raise ValueError("BaoStock V3 holdout dates are insufficient")
    if len(value.first_embargo_dates) != 5 or len(value.second_embargo_dates) != 5:
        raise ValueError("BaoStock V3 split requires two five-day embargoes")
    if len(value.early_stopping_dates) != 20 or len(value.calibration_dates) != 20:
        raise ValueError("BaoStock V3 split requires fixed early-stop and calibration dates")
    if value.model_fit_dates + value.early_stopping_dates + value.calibration_dates != value.development_dates:
        raise ValueError("BaoStock V3 development sub-splits are invalid")


def build_baostock_v3_split(
    dates: tuple[date, ...],
    *,
    parent_manifest_hash: str,
) -> BaoStockV3Split:
    ordered = tuple(dates)
    if ordered != tuple(sorted(set(ordered))) or len(ordered) < BAOSTOCK_MIN_V3_DATES:
        raise ValueError("BaoStock V3 split requires at least 1250 unique ordered dates")
    point_in_time = ordered[-BAOSTOCK_POINT_IN_TIME_RESERVE:]
    earlier = ordered[:-BAOSTOCK_POINT_IN_TIME_RESERVE]
    first_boundary = int(len(earlier) * 0.60)
    second_boundary = int(len(earlier) * 0.80)
    raw_development = earlier[:first_boundary]
    raw_confirmation = earlier[first_boundary:second_boundary]
    daily_proxy = earlier[second_boundary:]
    development = raw_development[:-5]
    first_embargo = raw_development[-5:]
    confirmation = raw_confirmation[:-5]
    second_embargo = raw_confirmation[-5:]
    model_fit = development[:-40]
    early_stopping = development[-40:-20]
    calibration = development[-20:]
    return BaoStockV3Split(
        parent_manifest_hash=parent_manifest_hash,
        label_contract=BaoStockV3LabelContract(),
        model_fit_dates=model_fit,
        early_stopping_dates=early_stopping,
        calibration_dates=calibration,
        development_dates=development,
        first_embargo_dates=first_embargo,
        confirmation_dates=confirmation,
        second_embargo_dates=second_embargo,
        daily_proxy_holdout_dates=daily_proxy,
        point_in_time_holdout_dates=point_in_time,
    )


@dataclass(frozen=True)
class BaoStockDailyManifest:
    spec_hash: str
    calendar_hash: str
    universe_hash: str
    logical_records_hash: str
    source_versions_hash: str
    source_versions: BaoStockSourceVersions
    catalog_sha256: str
    partitions: tuple[BaoStockPartitionRef, ...]
    audit: BaoStockCoverageAudit
    production_authority: bool = False
    point_in_time_parity: bool = False
    terminal_holdout_opened: bool = False
    schema_version: str = "baostock_daily_manifest_v3"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        hashes = (
            self.spec_hash,
            self.calendar_hash,
            self.universe_hash,
            self.logical_records_hash,
            self.source_versions_hash,
            self.catalog_sha256,
        )
        if any(_SHA256.fullmatch(value) is None for value in hashes):
            raise ValueError("BaoStock manifest hash is invalid")
        partitions = tuple(sorted(self.partitions, key=lambda item: item.relative_path))
        codes = tuple(code for item in partitions for code in item.codes)
        if self.audit.status == "coverage_ready" and (not partitions or len(codes) != len(set(codes))):
            raise ValueError("BaoStock manifest partitions are empty or overlap")
        if self.source_versions.content_hash != self.source_versions_hash:
            raise ValueError("BaoStock manifest source versions hash mismatch")
        if self.audit.spec_hash != self.spec_hash or self.audit.calendar_hash != self.calendar_hash:
            raise ValueError("BaoStock manifest audit parent mismatch")
        if self.audit.universe_hash != self.universe_hash:
            raise ValueError("BaoStock manifest universe parent mismatch")
        if self.production_authority or self.point_in_time_parity or self.terminal_holdout_opened:
            raise ValueError("BaoStock manifest cannot authorize production, parity, or holdouts")
        if self.schema_version != "baostock_daily_manifest_v3":
            raise ValueError("BaoStock manifest schema is invalid")
        object.__setattr__(self, "partitions", partitions)
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class BaoStockV3DatasetManifest:
    daily_manifest_hash: str
    effective_facts_hash: str
    label_contract: BaoStockV3LabelContract
    status: BaoStockV3DatasetStatus
    split: BaoStockV3Split | None
    failure_reasons: tuple[str, ...]
    point_in_time_parity: bool = False
    production_authority: bool = False
    terminal_holdout_opened: bool = False
    schema_version: str = "tomorrow_v3_baostock_dataset_v1"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if any(_SHA256.fullmatch(value) is None for value in (self.daily_manifest_hash, self.effective_facts_hash)):
            raise ValueError("BaoStock V3 dataset parent hash is invalid")
        reasons = tuple(sorted(set(self.failure_reasons)))
        if self.status == "dataset_ready":
            if reasons or self.split is None:
                raise ValueError("ready BaoStock V3 dataset requires one split and no failures")
            if self.split.parent_manifest_hash != self.daily_manifest_hash:
                raise ValueError("BaoStock V3 dataset split parent mismatch")
            if self.label_contract != self.split.label_contract:
                raise ValueError("BaoStock V3 dataset label contract does not match its split")
        elif self.status == "historical_data_insufficient":
            if not reasons or self.split is not None:
                raise ValueError("insufficient BaoStock V3 dataset requires failures and no split")
        else:
            raise ValueError("BaoStock V3 dataset status is invalid")
        if self.point_in_time_parity or self.production_authority or self.terminal_holdout_opened:
            raise ValueError("BaoStock V3 dataset cannot authorize parity, production, or open holdouts")
        if self.schema_version != "tomorrow_v3_baostock_dataset_v1":
            raise ValueError("BaoStock V3 dataset schema is invalid")
        object.__setattr__(self, "failure_reasons", reasons)
        object.__setattr__(self, "content_hash", canonical_hash(self))


def build_baostock_v3_dataset_manifest(
    daily: BaoStockDailyManifest,
    effective_facts: HistoricalEffectiveFactsAudit,
    complete_dates: tuple[date, ...],
) -> BaoStockV3DatasetManifest:
    reasons = set(daily.audit.failure_reasons)
    reasons.update(effective_facts.failure_reasons)
    if not reasons and len(complete_dates) < BAOSTOCK_MIN_V3_DATES:
        reasons.add("v3_complete_dates_below_1250")
    if reasons:
        return BaoStockV3DatasetManifest(
            daily.content_hash,
            effective_facts.content_hash,
            BaoStockV3LabelContract(),
            "historical_data_insufficient",
            None,
            tuple(reasons),
        )
    split = build_baostock_v3_split(complete_dates, parent_manifest_hash=daily.content_hash)
    return BaoStockV3DatasetManifest(
        daily.content_hash,
        effective_facts.content_hash,
        split.label_contract,
        "dataset_ready",
        split,
        (),
    )


__all__ = [
    "BAOSTOCK_MAX_SESSIONS",
    "BAOSTOCK_RESEARCH_IDENTITY",
    "BAOSTOCK_SOURCE_CUTOFF",
    "BaoStockAdjustment",
    "BaoStockBoard",
    "BaoStockBoardCoverage",
    "BaoStockCalendar",
    "BaoStockCellStatus",
    "BaoStockCodeBatch",
    "BaoStockCodeDownload",
    "BaoStockCodeCoverage",
    "BaoStockCoverageAudit",
    "BaoStockCoverageStatus",
    "BaoStockDailyCell",
    "BaoStockDailyManifest",
    "BaoStockDailyFact",
    "BaoStockDailySide",
    "BaoStockDailySpec",
    "BaoStockIndustryInterval",
    "BaoStockPartitionRef",
    "BaoStockSecurity",
    "BaoStockSourceVersions",
    "BaoStockTrainingRow",
    "BaoStockTradingStatus",
    "BaoStockV3LabelContract",
    "BaoStockV3DatasetManifest",
    "BaoStockV3DatasetStatus",
    "BaoStockV3Split",
    "build_baostock_coverage_audit",
    "build_baostock_v3_split",
    "build_baostock_v3_dataset_manifest",
    "join_baostock_daily_sides",
]

"""BaoStock daily artifact serialization helpers."""

from __future__ import annotations

from datetime import date
from typing import cast

from trader.domain.research.baostock_daily import (
    BaoStockAdjustment,
    BaoStockBoard,
    BaoStockBoardCoverage,
    BaoStockCalendar,
    BaoStockCellStatus,
    BaoStockCodeBatch,
    BaoStockCodeCoverage,
    BaoStockCoverageAudit,
    BaoStockCoverageStatus,
    BaoStockDailyCell,
    BaoStockDailyManifest,
    BaoStockDailySide,
    BaoStockDailySpec,
    BaoStockPartitionRef,
    BaoStockSecurity,
    BaoStockSourceVersions,
    BaoStockTradingStatus,
)
from trader.infra.research.baostock_daily_codec import (
    boolean as _boolean,
)
from trader.infra.research.baostock_daily_codec import (
    dates as _dates,
)
from trader.infra.research.baostock_daily_codec import (
    fields as _fields,
)
from trader.infra.research.baostock_daily_codec import (
    integer as _integer,
)
from trader.infra.research.baostock_daily_codec import (
    number as _number,
)
from trader.infra.research.baostock_daily_codec import (
    object_value as _object,
)
from trader.infra.research.baostock_daily_codec import (
    optional_number as _optional_number,
)
from trader.infra.research.baostock_daily_codec import (
    pairs as _pairs,
)
from trader.infra.research.baostock_daily_codec import (
    string as _string,
)
from trader.infra.research.baostock_daily_codec import (
    strings as _strings,
)


def _encode_spec(value: BaoStockDailySpec) -> dict[str, object]:
    return {
        "sessions": value.sessions,
        "research_identity": value.research_identity,
        "source_cutoff": value.source_cutoff.isoformat(),
        "production_authority": value.production_authority,
        "point_in_time_parity": value.point_in_time_parity,
        "schema_version": value.schema_version,
    }


def _decode_spec(raw: dict[str, object]) -> BaoStockDailySpec:
    _fields(
        raw,
        {
            "sessions",
            "research_identity",
            "source_cutoff",
            "production_authority",
            "point_in_time_parity",
            "schema_version",
        },
        "spec",
    )
    return BaoStockDailySpec(
        sessions=_integer(raw["sessions"]),
        research_identity=_string(raw["research_identity"]),
        source_cutoff=date.fromisoformat(_string(raw["source_cutoff"])),
        production_authority=_boolean(raw["production_authority"]),
        point_in_time_parity=_boolean(raw["point_in_time_parity"]),
        schema_version=_string(raw["schema_version"]),
    )


def _encode_calendar(value: BaoStockCalendar) -> dict[str, object]:
    return {
        "open_dates": [item.isoformat() for item in value.open_dates],
        "schema_version": value.schema_version,
    }


def _decode_calendar(raw: dict[str, object]) -> BaoStockCalendar:
    _fields(raw, {"open_dates", "schema_version"}, "calendar")
    return BaoStockCalendar(_dates(raw["open_dates"]), schema_version=_string(raw["schema_version"]))


def _encode_security(value: BaoStockSecurity) -> dict[str, object]:
    return {
        "code": value.code,
        "name": value.name,
        "board": value.board,
        "listed_on": value.listed_on.isoformat(),
        "delisted_on": value.delisted_on.isoformat() if value.delisted_on is not None else None,
        "source_version": value.source_version,
    }


def _decode_security(raw: dict[str, object]) -> BaoStockSecurity:
    _fields(raw, {"code", "name", "board", "listed_on", "delisted_on", "source_version"}, "security")
    delisted = raw["delisted_on"]
    if delisted is not None and not isinstance(delisted, str):
        raise TypeError("BaoStock security delisted_on is invalid")
    return BaoStockSecurity(
        _string(raw["code"]),
        _string(raw["name"]),
        cast(BaoStockBoard, _string(raw["board"])),
        date.fromisoformat(_string(raw["listed_on"])),
        date.fromisoformat(delisted) if delisted is not None else None,
        _string(raw["source_version"]),
    )


def _decode_universe(raw: list[object]) -> tuple[BaoStockSecurity, ...]:
    return tuple(_decode_security(_object(item, "security")) for item in raw)


def _encode_versions(value: BaoStockSourceVersions) -> dict[str, object]:
    return {
        "sdk_version": value.sdk_version,
        "python_version": value.python_version,
        "dependency_versions": [list(item) for item in value.dependency_versions],
    }


def _decode_versions(raw: dict[str, object]) -> BaoStockSourceVersions:
    _fields(raw, {"sdk_version", "python_version", "dependency_versions"}, "versions")
    return BaoStockSourceVersions(
        _string(raw["sdk_version"]),
        _string(raw["python_version"]),
        _pairs(raw["dependency_versions"]),
    )


def _encode_side(value: BaoStockDailySide) -> dict[str, object]:
    return {
        "code": value.code,
        "trade_date": value.trade_date.isoformat(),
        "adjustment": value.adjustment,
        "open_price": value.open_price,
        "high_price": value.high_price,
        "low_price": value.low_price,
        "close_price": value.close_price,
        "volume": value.volume,
        "amount": value.amount,
        "preclose": value.preclose,
        "pct_change": value.pct_change,
        "turnover": value.turnover,
        "trading_status": value.trading_status,
    }


def _decode_side(raw: dict[str, object]) -> BaoStockDailySide:
    expected = {
        "code",
        "trade_date",
        "adjustment",
        "open_price",
        "high_price",
        "low_price",
        "close_price",
        "volume",
        "amount",
        "preclose",
        "pct_change",
        "turnover",
        "trading_status",
    }
    _fields(raw, expected, "daily side")
    return BaoStockDailySide(
        _string(raw["code"]),
        date.fromisoformat(_string(raw["trade_date"])),
        cast(BaoStockAdjustment, _string(raw["adjustment"])),
        _optional_number(raw["open_price"]),
        _optional_number(raw["high_price"]),
        _optional_number(raw["low_price"]),
        _optional_number(raw["close_price"]),
        _optional_number(raw["volume"]),
        _optional_number(raw["amount"]),
        _optional_number(raw["preclose"]),
        _optional_number(raw["pct_change"]),
        _optional_number(raw["turnover"]),
        cast(BaoStockTradingStatus, _string(raw["trading_status"])),
    )


def _encode_cell(value: BaoStockDailyCell) -> dict[str, object]:
    return {
        "code": value.code,
        "trade_date": value.trade_date.isoformat(),
        "status": value.status,
        "unadjusted": _encode_side(value.unadjusted) if value.unadjusted is not None else None,
        "qfq": _encode_side(value.qfq) if value.qfq is not None else None,
    }


def _decode_cell(raw: dict[str, object]) -> BaoStockDailyCell:
    _fields(raw, {"code", "trade_date", "status", "unadjusted", "qfq"}, "daily cell")
    unadjusted = raw["unadjusted"]
    qfq = raw["qfq"]
    return BaoStockDailyCell(
        _string(raw["code"]),
        date.fromisoformat(_string(raw["trade_date"])),
        cast(BaoStockCellStatus, _string(raw["status"])),
        _decode_side(_object(unadjusted, "unadjusted")) if unadjusted is not None else None,
        _decode_side(_object(qfq, "qfq")) if qfq is not None else None,
    )


def _encode_batch_metadata(value: BaoStockCodeBatch) -> dict[str, object]:
    return {
        "duplicate_rows": value.duplicate_rows,
        "null_rows": value.null_rows,
        "out_of_window_rows": value.out_of_window_rows,
        "future_rows": value.future_rows,
        "failure_reasons": list(value.failure_reasons),
    }


def _decode_batch_metadata(
    code: str,
    raw: dict[str, object],
    cells: tuple[BaoStockDailyCell, ...],
) -> BaoStockCodeBatch:
    _fields(
        raw,
        {"duplicate_rows", "null_rows", "out_of_window_rows", "future_rows", "failure_reasons"},
        "batch metadata",
    )
    return BaoStockCodeBatch(
        code,
        cells,
        _integer(raw["duplicate_rows"]),
        _integer(raw["null_rows"]),
        _integer(raw["out_of_window_rows"]),
        _integer(raw["future_rows"]),
        _strings(raw["failure_reasons"]),
    )


def _encode_audit(value: BaoStockCoverageAudit) -> dict[str, object]:
    return {
        "spec_hash": value.spec_hash,
        "calendar_hash": value.calendar_hash,
        "universe_hash": value.universe_hash,
        "calendar_sessions": value.calendar_sessions,
        "calendar_first_date": value.calendar_first_date.isoformat(),
        "calendar_last_date": value.calendar_last_date.isoformat(),
        "universe_count": value.universe_count,
        "expected_cells": value.expected_cells,
        "obtained_cells": value.obtained_cells,
        "all_cell_coverage": value.all_cell_coverage,
        "board_coverages": [
            {
                "board": item.board,
                "expected_cells": item.expected_cells,
                "obtained_cells": item.obtained_cells,
                "coverage_ratio": item.coverage_ratio,
            }
            for item in value.board_coverages
        ],
        "code_coverages": [
            {
                "code": item.code,
                "expected_cells": item.expected_cells,
                "obtained_cells": item.obtained_cells,
                "coverage_ratio": item.coverage_ratio,
                "eligible_for_v3_population": item.eligible_for_v3_population,
            }
            for item in value.code_coverages
        ],
        "full_window_stock_count": value.full_window_stock_count,
        "full_window_stocks_at_95_percent": value.full_window_stocks_at_95_percent,
        "full_window_stock_success_ratio": value.full_window_stock_success_ratio,
        "failed_codes": list(value.failed_codes),
        "duplicate_rows": value.duplicate_rows,
        "null_rows": value.null_rows,
        "out_of_window_rows": value.out_of_window_rows,
        "future_rows": value.future_rows,
        "latest_reserved_dates": [item.isoformat() for item in value.latest_reserved_dates],
        "status": value.status,
        "failure_reasons": list(value.failure_reasons),
        "terminal_holdout_opened": value.terminal_holdout_opened,
        "production_authority": value.production_authority,
        "point_in_time_parity": value.point_in_time_parity,
        "schema_version": value.schema_version,
    }


def _decode_audit(raw: dict[str, object]) -> BaoStockCoverageAudit:
    expected = {
        "spec_hash",
        "calendar_hash",
        "universe_hash",
        "calendar_sessions",
        "calendar_first_date",
        "calendar_last_date",
        "universe_count",
        "expected_cells",
        "obtained_cells",
        "all_cell_coverage",
        "board_coverages",
        "code_coverages",
        "full_window_stock_count",
        "full_window_stocks_at_95_percent",
        "full_window_stock_success_ratio",
        "failed_codes",
        "duplicate_rows",
        "null_rows",
        "out_of_window_rows",
        "future_rows",
        "latest_reserved_dates",
        "status",
        "failure_reasons",
        "terminal_holdout_opened",
        "production_authority",
        "point_in_time_parity",
        "schema_version",
    }
    _fields(raw, expected, "coverage audit")
    return BaoStockCoverageAudit(
        spec_hash=_string(raw["spec_hash"]),
        calendar_hash=_string(raw["calendar_hash"]),
        universe_hash=_string(raw["universe_hash"]),
        calendar_sessions=_integer(raw["calendar_sessions"]),
        calendar_first_date=date.fromisoformat(_string(raw["calendar_first_date"])),
        calendar_last_date=date.fromisoformat(_string(raw["calendar_last_date"])),
        universe_count=_integer(raw["universe_count"]),
        expected_cells=_integer(raw["expected_cells"]),
        obtained_cells=_integer(raw["obtained_cells"]),
        all_cell_coverage=_number(raw["all_cell_coverage"]),
        board_coverages=_decode_board_coverages(raw["board_coverages"]),
        code_coverages=_decode_code_coverages(raw["code_coverages"]),
        full_window_stock_count=_integer(raw["full_window_stock_count"]),
        full_window_stocks_at_95_percent=_integer(raw["full_window_stocks_at_95_percent"]),
        full_window_stock_success_ratio=_number(raw["full_window_stock_success_ratio"]),
        failed_codes=_strings(raw["failed_codes"]),
        duplicate_rows=_integer(raw["duplicate_rows"]),
        null_rows=_integer(raw["null_rows"]),
        out_of_window_rows=_integer(raw["out_of_window_rows"]),
        future_rows=_integer(raw["future_rows"]),
        latest_reserved_dates=_dates(raw["latest_reserved_dates"]),
        status=cast(BaoStockCoverageStatus, _string(raw["status"])),
        failure_reasons=_strings(raw["failure_reasons"]),
        terminal_holdout_opened=_boolean(raw["terminal_holdout_opened"]),
        production_authority=_boolean(raw["production_authority"]),
        point_in_time_parity=_boolean(raw["point_in_time_parity"]),
        schema_version=_string(raw["schema_version"]),
    )


def _encode_manifest(value: BaoStockDailyManifest) -> dict[str, object]:
    return {
        "spec_hash": value.spec_hash,
        "calendar_hash": value.calendar_hash,
        "universe_hash": value.universe_hash,
        "logical_records_hash": value.logical_records_hash,
        "source_versions_hash": value.source_versions_hash,
        "source_versions": _encode_versions(value.source_versions),
        "catalog_sha256": value.catalog_sha256,
        "partitions": [
            {
                "relative_path": item.relative_path,
                "board": item.board,
                "code_prefix": item.code_prefix,
                "codes": list(item.codes),
                "row_count": item.row_count,
                "logical_records_hash": item.logical_records_hash,
                "database_sha256": item.database_sha256,
                "schema_version": item.schema_version,
            }
            for item in value.partitions
        ],
        "audit": _encode_audit(value.audit),
        "production_authority": value.production_authority,
        "point_in_time_parity": value.point_in_time_parity,
        "terminal_holdout_opened": value.terminal_holdout_opened,
        "schema_version": value.schema_version,
    }


def _decode_manifest(raw: dict[str, object]) -> BaoStockDailyManifest:
    expected = {
        "spec_hash",
        "calendar_hash",
        "universe_hash",
        "logical_records_hash",
        "source_versions_hash",
        "source_versions",
        "catalog_sha256",
        "partitions",
        "audit",
        "production_authority",
        "point_in_time_parity",
        "terminal_holdout_opened",
        "schema_version",
    }
    _fields(raw, expected, "manifest")
    return BaoStockDailyManifest(
        spec_hash=_string(raw["spec_hash"]),
        calendar_hash=_string(raw["calendar_hash"]),
        universe_hash=_string(raw["universe_hash"]),
        logical_records_hash=_string(raw["logical_records_hash"]),
        source_versions_hash=_string(raw["source_versions_hash"]),
        source_versions=_decode_versions(_object(raw["source_versions"], "source versions")),
        catalog_sha256=_string(raw["catalog_sha256"]),
        partitions=_decode_partition_refs(raw["partitions"]),
        audit=_decode_audit(_object(raw["audit"], "audit")),
        production_authority=_boolean(raw["production_authority"]),
        point_in_time_parity=_boolean(raw["point_in_time_parity"]),
        terminal_holdout_opened=_boolean(raw["terminal_holdout_opened"]),
        schema_version=_string(raw["schema_version"]),
    )


def _decode_partition_refs(value: object) -> tuple[BaoStockPartitionRef, ...]:
    if not isinstance(value, list):
        raise TypeError("BaoStock manifest partitions are invalid")
    result: list[BaoStockPartitionRef] = []
    for value_item in value:
        raw = _object(value_item, "partition reference")
        _fields(
            raw,
            {
                "relative_path",
                "board",
                "code_prefix",
                "codes",
                "row_count",
                "logical_records_hash",
                "database_sha256",
                "schema_version",
            },
            "partition reference",
        )
        result.append(
            BaoStockPartitionRef(
                relative_path=_string(raw["relative_path"]),
                board=cast(BaoStockBoard, _string(raw["board"])),
                code_prefix=_string(raw["code_prefix"]),
                codes=_strings(raw["codes"]),
                row_count=_integer(raw["row_count"]),
                logical_records_hash=_string(raw["logical_records_hash"]),
                database_sha256=_string(raw["database_sha256"]),
                schema_version=_string(raw["schema_version"]),
            )
        )
    return tuple(result)


def _decode_board_coverages(value: object) -> tuple[BaoStockBoardCoverage, ...]:
    if not isinstance(value, list):
        raise TypeError("BaoStock board coverage list is invalid")
    results: list[BaoStockBoardCoverage] = []
    for item in value:
        raw = _object(item, "board coverage")
        _fields(raw, {"board", "expected_cells", "obtained_cells", "coverage_ratio"}, "board coverage")
        results.append(
            BaoStockBoardCoverage(
                cast(BaoStockBoard, _string(raw["board"])),
                _integer(raw["expected_cells"]),
                _integer(raw["obtained_cells"]),
                _number(raw["coverage_ratio"]),
            )
        )
    return tuple(results)


def _decode_code_coverages(value: object) -> tuple[BaoStockCodeCoverage, ...]:
    if not isinstance(value, list):
        raise TypeError("BaoStock code coverage list is invalid")
    results: list[BaoStockCodeCoverage] = []
    for item in value:
        raw = _object(item, "code coverage")
        _fields(
            raw,
            {"code", "expected_cells", "obtained_cells", "coverage_ratio", "eligible_for_v3_population"},
            "code coverage",
        )
        results.append(
            BaoStockCodeCoverage(
                _string(raw["code"]),
                _integer(raw["expected_cells"]),
                _integer(raw["obtained_cells"]),
                _number(raw["coverage_ratio"]),
                _boolean(raw["eligible_for_v3_population"]),
            )
        )
    return tuple(results)

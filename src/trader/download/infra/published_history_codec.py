"""Decode only daily facts after the archive reader verifies a sealed partition."""

from __future__ import annotations

import json
import math
from datetime import date
from typing import cast

from trader.download.domain.baostock_daily import BaoStockAdjustment, BaoStockCellStatus, BaoStockTradingStatus
from trader.download.domain.published_history import PublishedHistoryCell, PublishedHistorySide


def encode_published_history_cell(cell: PublishedHistoryCell) -> str:
    return json.dumps(
        {
            "code": cell.code,
            "trade_date": cell.trade_date.isoformat(),
            "status": cell.status,
            "unadjusted": _encode_side(cell.unadjusted),
            "qfq": _encode_side(cell.qfq),
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def decode_published_history_cell_payload(code: str, trade_date: str, payload: str) -> PublishedHistoryCell:
    value = json.loads(payload)
    if not isinstance(value, dict) or value.get("code") != code or value.get("trade_date") != trade_date:
        raise ValueError("published history cell payload identity is invalid")
    return decode_published_history_cell((trade_date, code, payload))


def decode_published_history_cell(row: tuple[object, ...]) -> PublishedHistoryCell:
    """The SQL projection excludes industry/ST and all full-revision hashing."""
    if len(row) != 3:
        raise ValueError("published daily row shape is invalid")
    trade_date, code, cell_json = row
    if not isinstance(trade_date, str) or not isinstance(code, str) or not isinstance(cell_json, str):
        raise ValueError("published daily row fields are invalid")
    cell = _object(json.loads(cell_json))
    if set(cell) != {"code", "trade_date", "status", "unadjusted", "qfq"}:
        raise ValueError("published daily cell fields are invalid")
    if cell["code"] != code or cell["trade_date"] != trade_date:
        raise ValueError("published daily row identity is invalid")
    status = _text(cell["status"])
    if status not in {"complete", "supplier_marked_suspended", "unadjusted_missing", "qfq_missing", "unknown_missing"}:
        raise ValueError("published daily status is invalid")
    return PublishedHistoryCell(
        code,
        date.fromisoformat(trade_date),
        cast(BaoStockCellStatus, status),
        _side(cell["unadjusted"], "unadjusted"),
        _side(cell["qfq"], "qfq"),
    )


def _side(value: object, adjustment: BaoStockAdjustment) -> PublishedHistorySide | None:
    if value is None:
        return None
    payload = _object(value)
    if set(payload) != {
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
    }:
        raise ValueError("published daily side fields are invalid")
    status = _text(payload["trading_status"])
    if payload["adjustment"] != adjustment or status not in {"trading", "suspended"}:
        raise ValueError("published daily side semantics are invalid")
    return PublishedHistorySide(
        _text(payload["code"]),
        date.fromisoformat(_text(payload["trade_date"])),
        adjustment,
        _number(payload["open_price"]),
        _number(payload["high_price"]),
        _number(payload["low_price"]),
        _number(payload["close_price"]),
        _number(payload["volume"]),
        _number(payload["amount"]),
        _number(payload["preclose"]),
        _number(payload["pct_change"]),
        _number(payload["turnover"]),
        cast(BaoStockTradingStatus, status),
    )


def _encode_side(value: PublishedHistorySide | None) -> dict[str, object] | None:
    if value is None:
        return None
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


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("published daily object is invalid")
    return cast(dict[str, object], value)


def _text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("published daily text is invalid")
    return value


def _number(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError("published daily number is invalid")
    return float(value)

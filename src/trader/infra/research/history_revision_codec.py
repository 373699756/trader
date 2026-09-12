"""Strict SQLite payload codec for monthly historical revisions."""

from __future__ import annotations

import json
from datetime import date
from typing import Literal, cast

from trader.domain.research.baostock_daily import BaoStockDailyCell, BaoStockDailySide
from trader.domain.research.history_revision import HistoryRevision


def encode_history_revision(value: HistoryRevision) -> str:
    payload = {
        "first_seen_sequence": value.first_seen_sequence,
        "board": value.board,
        "cell": {
            "code": value.cell.code,
            "trade_date": value.cell.trade_date.isoformat(),
            "status": value.cell.status,
            "unadjusted": _encode_side(value.cell.unadjusted),
            "qfq": _encode_side(value.cell.qfq),
        },
        "is_st": value.is_st,
        "industry": value.industry,
        "industry_classification": value.industry_classification,
    }
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def decode_history_revision(payload_json: str) -> HistoryRevision:
    try:
        payload = _object(json.loads(payload_json), "revision")
        _keys(
            payload,
            {
                "first_seen_sequence",
                "board",
                "cell",
                "is_st",
                "industry",
                "industry_classification",
            },
            "revision",
        )
        cell_payload = _object(payload["cell"], "cell")
        _keys(cell_payload, {"code", "trade_date", "status", "unadjusted", "qfq"}, "cell")
        cell = BaoStockDailyCell(
            _text(cell_payload["code"], "code"),
            date.fromisoformat(_text(cell_payload["trade_date"], "trade date")),
            cast(
                Literal[
                    "complete",
                    "supplier_marked_suspended",
                    "unadjusted_missing",
                    "qfq_missing",
                    "unknown_missing",
                ],
                _text(cell_payload["status"], "cell status"),
            ),
            _decode_optional_side(cell_payload["unadjusted"], "unadjusted"),
            _decode_optional_side(cell_payload["qfq"], "qfq"),
        )
        return HistoryRevision(
            _integer(payload["first_seen_sequence"], "first seen sequence"),
            cast(Literal["main", "chinext", "star"], _text(payload["board"], "board")),
            cell,
            _optional_boolean(payload["is_st"]),
            _optional_text(payload["industry"], "industry"),
            _optional_text(payload["industry_classification"], "industry classification"),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("history monthly revision payload is invalid") from exc


def _encode_side(value: BaoStockDailySide | None) -> dict[str, object] | None:
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


def _decode_optional_side(value: object, label: str) -> BaoStockDailySide | None:
    if value is None:
        return None
    payload = _object(value, label)
    _keys(
        payload,
        {
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
        },
        label,
    )
    return BaoStockDailySide(
        _text(payload["code"], "code"),
        date.fromisoformat(_text(payload["trade_date"], "trade date")),
        cast(Literal["unadjusted", "qfq"], _text(payload["adjustment"], "adjustment")),
        _optional_number(payload["open_price"], "open price"),
        _optional_number(payload["high_price"], "high price"),
        _optional_number(payload["low_price"], "low price"),
        _optional_number(payload["close_price"], "close price"),
        _optional_number(payload["volume"], "volume"),
        _optional_number(payload["amount"], "amount"),
        _optional_number(payload["preclose"], "preclose"),
        _optional_number(payload["pct_change"], "pct change"),
        _optional_number(payload["turnover"], "turnover"),
        cast(Literal["trading", "suspended"], _text(payload["trading_status"], "trading status")),
    )


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TypeError(f"history monthly {label} must be an object")
    return cast(dict[str, object], value)


def _keys(payload: dict[str, object], expected: set[str], label: str) -> None:
    if set(payload) != expected:
        raise ValueError(f"history monthly {label} fields are invalid")


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError(f"history monthly {label} must be text")
    return value


def _optional_text(value: object, label: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise TypeError(f"history monthly {label} must be optional text")
    return value


def _integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"history monthly {label} must be integer")
    return value


def _optional_boolean(value: object) -> bool | None:
    if value is not None and not isinstance(value, bool):
        raise TypeError("history monthly ST fact must be optional boolean")
    return value


def _optional_number(value: object, label: str) -> float | None:
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"history monthly {label} must be optional numeric")
    return float(value)


__all__ = ["decode_history_revision", "encode_history_revision"]

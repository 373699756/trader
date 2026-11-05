"""Bounded Eastmoney field observations; source rows never imply event identity."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from trader.download.domain.company_evidence import EvidenceMarket
from trader.download.domain.company_structured_fields import (
    CompanyContractObservation,
    CompanyHolderTradeObservation,
    CompanyRepurchaseObservation,
    CompanyStructuredDataset,
    CompanyStructuredObservation,
    CompanyStructuredPage,
)

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_CODE_COLUMNS = {
    CompanyStructuredDataset.REPURCHASE: "DIM_SCODE",
    CompanyStructuredDataset.HOLDER_TRADE: "SECURITY_CODE",
    CompanyStructuredDataset.CONTRACT: "SECURITYCODE",
}
_DATE_COLUMNS = {
    CompanyStructuredDataset.REPURCHASE: "UPDATEDATE",
    CompanyStructuredDataset.HOLDER_TRADE: "NOTICE_DATE",
    CompanyStructuredDataset.CONTRACT: "DIM_RDATE",
}


def DecodeCompanyStructuredFields(
    dataset: CompanyStructuredDataset,
    payload: object,
    *,
    observed_at: datetime,
    window_start: date,
    window_end: date,
    page: int = 1,
    page_size: int = 500,
    eligible_codes: tuple[str, ...] = (),
    response_hash: str | None = None,
) -> CompanyStructuredPage:
    """Validate one source page; unknown event/version/units remain explicit.

    The response count refers to source rows, before population filtering.
    Hashes bind the exact decoded row, including fields not interpreted here;
    no raw JSON or mutable supplier object crosses the parsing boundary.
    """
    _validate_window(observed_at, window_start, window_end)
    if type(page) is not int or page < 1 or type(page_size) is not int or not 1 <= page_size <= 500:
        raise ValueError("company_structured_query_invalid")
    _validate_codes(eligible_codes)
    if (
        not isinstance(payload, Mapping) or payload.get("success") is not True
        or type(payload.get("code")) is not int or payload.get("code") != 0
    ):
        raise ValueError("company_structured_source_rejected")
    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise ValueError("company_structured_result_invalid")
    count, pages, rows = result.get("count"), result.get("pages"), result.get("data")
    if type(count) is not int or type(pages) is not int or count < 0 or pages < 0:
        raise ValueError("company_structured_pagination_invalid")
    if count == 0 and rows is None:
        rows = []
    if not isinstance(rows, list) or len(rows) > page_size or any(not isinstance(row, Mapping) for row in rows):
        raise ValueError("company_structured_rows_invalid")
    observations: list[CompanyStructuredObservation] = []
    for row in rows:
        code = row.get(_CODE_COLUMNS[dataset])
        if not isinstance(code, str):
            raise ValueError("company_structured_code_invalid")
        EvidenceMarket(code)
        selected_at = _timestamp(row.get(_DATE_COLUMNS[dataset]))
        if selected_at is None or not window_start <= selected_at.date() <= window_end:
            raise ValueError("company_structured_date_filter_ignored")
        if eligible_codes and code not in eligible_codes:
            continue
        observations.append(_observation(dataset, row, code, observed_at))
    return CompanyStructuredPage(
        dataset, page, page_size, pages, count, len(rows), tuple(observations), response_hash or _digest(payload),
    )


def _observation(
    dataset: CompanyStructuredDataset,
    row: Mapping[str, object],
    code: str,
    observed_at: datetime,
) -> CompanyStructuredObservation:
    if dataset is CompanyStructuredDataset.REPURCHASE:
        measurements = CompanyRepurchaseObservation(
            _number(row.get("REPURNUM")), _number(row.get("REPURAMOUNT")),
            _text(row.get("REPUROBJECTIVE")), _text(row.get("REPURPROGRESS")),
            _number(row.get("ZSZXX")), _number(row.get("ZSZSX")),
        )
        return CompanyStructuredObservation(
            dataset, code, _identifier(row.get("REPURCODE")), _timestamp(row.get("DIM_TRADEDATE")),
            _timestamp(row.get("UPDATEDATE")), observed_at, _digest(row), measurements,
        )
    if dataset is CompanyStructuredDataset.HOLDER_TRADE:
        raw_quantity = _number(row.get("CHANGE_NUM_SYMBOL"), signed=True)
        return CompanyStructuredObservation(
            dataset, code, None, _timestamp(row.get("NOTICE_DATE")), None, observed_at, _digest(row),
            CompanyHolderTradeObservation(
                _text(row.get("HOLDER_NAME")), _text(row.get("DIRECTION")),
                raw_quantity * 10_000 if raw_quantity is not None else None,
                _number(row.get("AFTER_CHANGE_RATE")), _day(row.get("START_DATE")), _day(row.get("END_DATE")),
            ),
        )
    raw_year = row.get("YEAR")
    year = int(raw_year) if isinstance(raw_year, str) and raw_year.isdigit() else raw_year
    if year is not None and type(year) is not int:
        raise ValueError("company_structured_year_invalid")
    return CompanyStructuredObservation(
        dataset, code, None, _timestamp(row.get("DIM_RDATE")), _timestamp(row.get("UPDATEDATE")),
        observed_at, _digest(row),
        CompanyContractObservation(
            _number(row.get("AMOUNTS")), _number(row.get("SNDYYSR")),
            _text(row.get("SIGNATORY")), _text(row.get("SIGNATORYRELNAME")),
            _text(row.get("CONTRACTNAME")), _text(row.get("COUNTERPARTY")), _text(row.get("SIGNEFFECT")),
            _text(row.get("ISABOLISHED")), _day(row.get("SIGNDATE")), year,
        ),
    )


def _validate_window(observed_at: datetime, start: date, end: date) -> None:
    if observed_at.utcoffset() is None:
        raise ValueError("company_structured_window_invalid")
    today = observed_at.astimezone(_SHANGHAI).date()
    if not today - timedelta(days=365) <= start <= end <= today:
        raise ValueError("company_structured_window_invalid")


def _validate_codes(codes: tuple[str, ...]) -> None:
    if len(codes) > 128 or len(set(codes)) != len(codes):
        raise ValueError("company_structured_population_invalid")
    for code in codes:
        EvidenceMarket(code)


def _number(value: object, *, signed: bool = False) -> float | None:
    if value is None or value in ("", "-", "--"):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("company_structured_number_invalid")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError("company_structured_number_invalid") from exc
    if not math.isfinite(number) or not signed and number < 0:
        raise ValueError("company_structured_number_invalid")
    return number


def _text(value: object) -> str | None:
    if value is None or value in ("", "-", "--"):
        return None
    if not isinstance(value, str) or len(value) > 4000:
        raise ValueError("company_structured_label_invalid")
    return value


def _identifier(value: object) -> str | None:
    if type(value) is int:
        return str(value)
    return _text(value)


def _timestamp(value: object) -> datetime | None:
    text = _text(value)
    if text is None:
        return None
    try:
        at = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("company_structured_date_invalid") from exc
    return at.replace(tzinfo=_SHANGHAI) if at.utcoffset() is None else at.astimezone(_SHANGHAI)


def _day(value: object) -> date | None:
    at = _timestamp(value)
    return at.date() if at is not None else None


def _digest(value: object) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("company_structured_payload_invalid") from exc
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "DecodeCompanyStructuredFields",
]

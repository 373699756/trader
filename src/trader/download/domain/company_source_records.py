"""Source-native financial values and official directory metadata, without revision claims."""

import re
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Literal, TypeAlias

from trader.download.domain.company_evidence import CompanyFinancial, EvidenceMarket
from trader.infra.serialization.canonical_codec import CanonicalDigest

CompanyFinancialField: TypeAlias = Literal[
    "basic_eps", "book_value_per_share", "revenue_growth_pct", "net_profit_growth_pct",
    "core_profit_growth_pct", "roe_pct", "parent_net_profit", "core_net_profit",
]
CompanyFinancialUnit: TypeAlias = Literal["currency_per_share", "percentage_points", "currency"]


def CompanyFinancialSourceValues(financial: CompanyFinancial) -> tuple[tuple[CompanyFinancialField, float | None], ...]:
    return (
        ("basic_eps", financial.basic_eps), ("book_value_per_share", financial.book_value_per_share),
        ("revenue_growth_pct", financial.revenue_growth_pct), ("net_profit_growth_pct", financial.net_profit_growth_pct),
        ("core_profit_growth_pct", financial.core_profit_growth_pct), ("roe_pct", financial.roe_pct),
        ("parent_net_profit", financial.parent_net_profit), ("core_net_profit", financial.core_net_profit),
    )


def CompanyFinancialSourceRecordHash(
    financial: CompanyFinancial,
    source_security_identity: str,
    source_notice_at: datetime,
    source_updated_at: datetime | None,
    currency: str,
) -> str:
    """Own the normalized content identity, without observation time or a revision id."""
    report_type = {(3, 31): "一季报", (6, 30): "中报", (9, 30): "三季报", (12, 31): "年报"}.get(
        (financial.report_date.month, financial.report_date.day),
    )
    if report_type is None:
        raise ValueError("company_financial_source_report_date_invalid")
    normalized = replace(
        financial,
        basic_eps=_number(financial.basic_eps),
        book_value_per_share=_number(financial.book_value_per_share),
        revenue_growth_pct=_number(financial.revenue_growth_pct),
        net_profit_growth_pct=_number(financial.net_profit_growth_pct),
        core_profit_growth_pct=_number(financial.core_profit_growth_pct),
        roe_pct=_number(financial.roe_pct),
        parent_net_profit=_number(financial.parent_net_profit),
        core_net_profit=_number(financial.core_net_profit),
    )
    return CanonicalDigest((source_security_identity, normalized, source_notice_at, source_updated_at, currency, report_type))


def _number(value: float | None) -> float | None:
    # SQLite REAL and JSON numeric decoders may change the Python numeric type
    # or drop a zero sign without changing the financial value.
    return None if value is None else 0.0 if value == 0 else float(value)


@dataclass(frozen=True, slots=True)
class CompanyFinancialMissingField:
    field: CompanyFinancialField
    reason: Literal["source_null"] = "source_null"

    def __post_init__(self) -> None:
        if self.field not in {
            "basic_eps", "book_value_per_share", "revenue_growth_pct", "net_profit_growth_pct",
            "core_profit_growth_pct", "roe_pct", "parent_net_profit", "core_net_profit",
        } or self.reason != "source_null":
            raise ValueError("company_financial_missing_field_invalid")


@dataclass(frozen=True, slots=True)
class CompanyFinancialSourceObservation:
    financial: CompanyFinancial
    source_security_identity: str
    source_notice_at: datetime
    source_updated_at: datetime | None
    observed_at: datetime
    currency: str
    record_hash: str
    missing_fields: tuple[CompanyFinancialMissingField, ...]

    def __post_init__(self) -> None:
        market = {"sse": "SH", "szse": "SZ", "bse": "BJ"}[EvidenceMarket(self.financial.code)]
        missing = tuple(CompanyFinancialMissingField(field) for field, value in CompanyFinancialSourceValues(self.financial)
                        if value is None)
        if (
            self.source_security_identity != f"{self.financial.code}.{market}" or self.currency != "CNY"
            or re.fullmatch(r"[0-9a-f]{64}", self.record_hash) is None or self.missing_fields != missing
            or self.observed_at.utcoffset() is None or self.source_notice_at.utcoffset() is None
            or self.source_updated_at is not None and self.source_updated_at.utcoffset() is None
        ):
            raise ValueError("company_financial_source_observation_invalid")
        if (self.source_notice_at > self.observed_at
            or self.financial.published_at > self.observed_at
            or self.source_updated_at is not None and self.source_updated_at > self.observed_at
            or self.financial.report_date > self.source_notice_at.date()
            or self.financial.published_at.date() != self.source_notice_at.date()):
            raise ValueError("company_financial_source_time_invalid")
        if self.record_hash != CompanyFinancialSourceRecordHash(
            self.financial, self.source_security_identity, self.source_notice_at, self.source_updated_at, self.currency,
        ):
            raise ValueError("company_financial_source_hash_mismatch")

    @property
    def business_key(self) -> tuple[str, date]:
        # This is a report lookup key, not an upstream record or revision id.
        return self.financial.code, self.financial.report_date

    @property
    def units(self) -> tuple[tuple[CompanyFinancialField, CompanyFinancialUnit], ...]:
        return (
            ("basic_eps", "currency_per_share"), ("book_value_per_share", "currency_per_share"),
            ("revenue_growth_pct", "percentage_points"), ("net_profit_growth_pct", "percentage_points"),
            ("core_profit_growth_pct", "percentage_points"), ("roe_pct", "percentage_points"),
            ("parent_net_profit", "currency"), ("core_net_profit", "currency"),
        )


@dataclass(frozen=True, slots=True)
class CompanyFinancialSourcePage:
    page: int
    pages: int
    total: int
    raw_count: int
    observations: tuple[CompanyFinancialSourceObservation, ...]
    response_hash: str

    def __post_init__(self) -> None:
        if (
            any(type(value) is not int for value in (self.page, self.pages, self.total, self.raw_count))
            or not 1 <= self.page <= self.pages <= 256 or not 0 <= self.raw_count <= 500
            or not 0 <= self.total <= self.pages * 500 or self.raw_count > self.total
            or self.total == 0 and self.pages != 1
            or self.total > 0 and self.pages > self.total
            or self.pages == 1 and self.raw_count != self.total
            or len(self.observations) > self.raw_count
            or self.total > 0 and self.raw_count == 0
            or re.fullmatch(r"[0-9a-f]{64}", self.response_hash) is None
            or len({item.business_key for item in self.observations}) != len(self.observations)
        ):
            raise ValueError("company_financial_source_page_invalid")


@dataclass(frozen=True, slots=True)
class CompanyOfficialDirectoryQuery:
    start_day: date
    end_day: date
    category: Literal["010301", "010303", "010305", "010307"]
    page: int = 1
    page_size: int = 20

    def __post_init__(self) -> None:
        if (not 0 <= (self.end_day - self.start_day).days < 365
            or self.category not in {"010301", "010303", "010305", "010307"}
            or type(self.page) is not int or not 1 <= self.page <= 256
            or type(self.page_size) is not int or not 1 <= self.page_size <= 50):
            raise ValueError("company_official_directory_query_invalid")


@dataclass(frozen=True, slots=True)
class CompanyOfficialDirectoryObservation:
    """Actual SZSE metadata; an attachment path is not a fetched or qualified original."""

    identity: str
    announcement_id: int
    codes: tuple[str, ...]
    title: str
    published_at: datetime
    attachment_path: str
    attachment_format: str


@dataclass(frozen=True, slots=True)
class CompanyOfficialDirectoryPage:
    query: CompanyOfficialDirectoryQuery
    total: int
    observations: tuple[CompanyOfficialDirectoryObservation, ...]
    response_hash: str

    @property
    def has_more(self) -> bool:
        return self.query.page * self.query.page_size < self.total


__all__ = [
    "CompanyFinancialField",
    "CompanyFinancialUnit",
    "CompanyFinancialMissingField",
    "CompanyFinancialSourceValues",
    "CompanyFinancialSourceRecordHash",
    "CompanyFinancialSourceObservation",
    "CompanyFinancialSourcePage",
    "CompanyOfficialDirectoryQuery",
    "CompanyOfficialDirectoryObservation",
    "CompanyOfficialDirectoryPage",
]

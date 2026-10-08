"""Lightweight issuer identity, independent of quotes and historical features."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime

from trader.recommendation.domain.market.models import Board


@dataclass(frozen=True, slots=True)
class StaticIssuer:
    code: str
    name: str
    board: Board
    exchange: str
    listing_date: date | None
    is_st: bool
    is_blacklisted: bool


@dataclass(frozen=True, slots=True)
class StaticMarketReference:
    """One accepted complete official population, independent of quote coverage."""

    records: tuple[StaticIssuer, ...]
    data_version: str
    source_time: datetime


def normalize_static_issuer(issuer: StaticIssuer) -> StaticIssuer | None:
    if len(issuer.code) != 6 or not issuer.code.isascii() or not issuer.code.isdigit() or not issuer.name.strip():
        return None
    return replace(issuer, name=issuer.name.strip())


__all__ = ["StaticIssuer", "StaticMarketReference", "normalize_static_issuer"]

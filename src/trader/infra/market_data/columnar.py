"""Typed Polars batches and deterministic dirty-set projection for P1-P3."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

import polars as pl
from polars.datatypes import DataType, DataTypeClass

from trader.application.cache import canonical_json_bytes
from trader.domain.market.models import CanonicalMarketSnapshot, MarketQuote


@dataclass(frozen=True)
class ColumnarBatchIdentity:
    dataset: str
    merge_epoch: str
    board_policy_version: str
    strategy_version: str
    config_version: str
    schema_version: str

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.__dict__)).hexdigest()


@dataclass(frozen=True)
class MarketChangeSet:
    merge_epoch: str
    inserted_codes: tuple[str, ...]
    updated_codes: tuple[str, ...]
    removed_codes: tuple[str, ...]

    @property
    def dirty_codes(self) -> tuple[str, ...]:
        return tuple(sorted((*self.inserted_codes, *self.updated_codes, *self.removed_codes)))


@dataclass(frozen=True)
class ColumnarQuoteBatch:
    identity: ColumnarBatchIdentity
    frame: pl.DataFrame

    @classmethod
    def from_snapshot(
        cls,
        snapshot: CanonicalMarketSnapshot,
        *,
        config_version: str,
        schema_version: str,
    ) -> ColumnarQuoteBatch:
        rows = [_quote_row(quote) for quote in snapshot.quotes]
        frame = pl.DataFrame(rows, schema=_QUOTE_SCHEMA, orient="row")
        return cls(
            ColumnarBatchIdentity(
                dataset="canonical_market_snapshot",
                merge_epoch=snapshot.merge_epoch,
                board_policy_version="not_applicable",
                strategy_version="not_applicable",
                config_version=config_version,
                schema_version=schema_version,
            ),
            frame.sort("code"),
        )


def market_changes(
    previous: ColumnarQuoteBatch | None,
    current: ColumnarQuoteBatch,
) -> MarketChangeSet:
    if previous is None or previous.frame.is_empty():
        return MarketChangeSet(current.identity.merge_epoch, tuple(current.frame["code"]), (), ())
    comparable = ("price", "pct_change", "amount", "turnover_rate", "data_version", "source_time")
    old = previous.frame.select("code", *comparable)
    new = current.frame.select("code", *comparable)
    joined = new.join(old, on="code", how="full", suffix="_old", coalesce=True)
    inserted = joined.filter(pl.col("data_version_old").is_null()).get_column("code").to_list()
    old_codes = set(old.get_column("code").to_list())
    new_codes = set(new.get_column("code").to_list())
    shared = joined.filter(pl.col("data_version").is_not_null() & pl.col("data_version_old").is_not_null())
    changed_expr = pl.any_horizontal(*[pl.col(name).ne_missing(pl.col(f"{name}_old")) for name in comparable])
    updated = shared.filter(changed_expr).get_column("code").to_list()
    return MarketChangeSet(
        current.identity.merge_epoch,
        tuple(sorted(str(code) for code in inserted)),
        tuple(sorted(str(code) for code in updated)),
        tuple(sorted(old_codes - new_codes)),
    )


_QUOTE_SCHEMA: dict[str, DataTypeClass | DataType] = {
    "code": pl.String,
    "price": pl.Float64,
    "pct_change": pl.Float64,
    "amount": pl.Float64,
    "turnover_rate": pl.Float64,
    "data_version": pl.String,
    "source_time": pl.Datetime(time_unit="us", time_zone="UTC"),
}


def _quote_row(quote: MarketQuote) -> tuple[str, float | None, float | None, float | None, float | None, str, datetime]:
    return (
        quote.code,
        quote.price,
        quote.pct_change,
        quote.amount,
        quote.turnover_rate,
        quote.data_version,
        quote.source_time,
    )


__all__ = ["ColumnarBatchIdentity", "ColumnarQuoteBatch", "MarketChangeSet", "market_changes"]

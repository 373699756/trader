"""Typed application values for offline Tomorrow point-in-time features."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime

from trader.domain.research.historical import SUPPORTED_RESEARCH_BOARDS, ResearchBoard
from trader.domain.research.tomorrow_features import PointInTimePublishedFact, TomorrowStockFeatures

TOMORROW_FEATURE_SCHEMA_VERSION = "score_tomorrow_point_in_time_features_v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SHANGHAI_TIMEZONE = "Asia/Shanghai"


@dataclass(frozen=True)
class TomorrowFeatureContext:
    code: str
    board: ResearchBoard
    industry: str
    industry_effective_at: datetime
    industry_received_at: datetime
    observed_at: datetime
    current_open: float
    current_high: float
    current_low: float
    current_last: float
    market_cap: float | None
    liquidity: float | None
    published_facts: tuple[PointInTimePublishedFact, ...] = ()

    def __post_init__(self) -> None:
        _validate_context_identity(self)
        _validate_context_market(self)
        facts = tuple(
            sorted(
                self.published_facts,
                key=lambda item: (item.kind, item.name, item.published_at, item.source, item.evidence_hash),
            )
        )
        _validate_context_facts(facts, self.observed_at)
        object.__setattr__(self, "industry", self.industry.strip())
        object.__setattr__(self, "published_facts", facts)


def _validate_context_identity(context: TomorrowFeatureContext) -> None:
    if len(context.code) != 6 or not context.code.isdigit():
        raise ValueError("Tomorrow feature context code is invalid")
    if context.board not in SUPPORTED_RESEARCH_BOARDS or not context.industry.strip():
        raise ValueError("Tomorrow feature context security identity is invalid")
    for value, label in (
        (context.industry_effective_at, "industry effective time"),
        (context.industry_received_at, "industry received time"),
        (context.observed_at, "feature context cutoff"),
    ):
        _require_shanghai(value, label)
    if context.industry_effective_at > context.observed_at or context.industry_received_at > context.observed_at:
        raise ValueError("industry identity is after feature cutoff")
    if context.industry_received_at < context.industry_effective_at:
        raise ValueError("industry identity cannot be received before it is effective")


def _validate_context_market(context: TomorrowFeatureContext) -> None:
    for price in (context.current_open, context.current_high, context.current_low, context.current_last):
        if not math.isfinite(price) or price <= 0.0:
            raise ValueError("Tomorrow feature context prices must be finite and positive")
    if context.current_low > min(context.current_open, context.current_last) or context.current_high < max(
        context.current_open, context.current_last
    ):
        raise ValueError("Tomorrow feature context OHLC is inconsistent")
    for control in (context.market_cap, context.liquidity):
        if control is not None and (not math.isfinite(control) or control <= 0.0):
            raise ValueError("Tomorrow feature context controls must be positive")


def _validate_context_facts(facts: tuple[PointInTimePublishedFact, ...], observed_at: datetime) -> None:
    if any(item.published_at > observed_at for item in facts):
        raise ValueError("published feature was published after feature cutoff")
    if any(item.received_at > observed_at for item in facts):
        raise ValueError("published feature was received after feature cutoff")


@dataclass(frozen=True)
class TomorrowFeatureContextBatch:
    trade_date: date
    input_hash: str
    contexts: tuple[TomorrowFeatureContext, ...]
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if _SHA256.fullmatch(self.input_hash) is None:
            raise ValueError("Tomorrow feature context input hash is invalid")
        contexts = tuple(sorted(self.contexts, key=lambda item: item.code))
        if not contexts or len({item.code for item in contexts}) != len(contexts):
            raise ValueError("Tomorrow feature contexts must be non-empty and unique")
        if any(item.observed_at.date() != self.trade_date for item in contexts):
            raise ValueError("Tomorrow feature context date is invalid")
        object.__setattr__(self, "contexts", contexts)
        object.__setattr__(self, "content_hash", _context_hash(self, contexts))


@dataclass(frozen=True)
class TomorrowPointInTimeFeatureBatch:
    trade_date: date
    observed_at: datetime
    input_hash: str
    context_hash: str
    rows: tuple[TomorrowStockFeatures, ...]
    production_authority: bool = False
    schema_version: str = TOMORROW_FEATURE_SCHEMA_VERSION
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_shanghai(self.observed_at, "Tomorrow feature batch cutoff")
        if (
            self.observed_at.date() != self.trade_date
            or _SHA256.fullmatch(self.input_hash) is None
            or _SHA256.fullmatch(self.context_hash) is None
        ):
            raise ValueError("Tomorrow feature batch identity is invalid")
        if self.schema_version != TOMORROW_FEATURE_SCHEMA_VERSION or self.production_authority:
            raise ValueError("Tomorrow feature batch cannot have production authority")
        rows = tuple(sorted(self.rows, key=lambda item: item.code))
        if not rows or len({item.code for item in rows}) != len(rows):
            raise ValueError("Tomorrow feature rows must be non-empty and unique")
        if any(item.as_of > self.observed_at or item.as_of.date() != self.trade_date for item in rows):
            raise ValueError("Tomorrow feature row cutoff is invalid")
        object.__setattr__(self, "rows", rows)
        object.__setattr__(self, "content_hash", _content_hash(self, rows))


def _content_hash(
    batch: TomorrowPointInTimeFeatureBatch,
    rows: tuple[TomorrowStockFeatures, ...],
) -> str:
    payload = {
        "schema_version": batch.schema_version,
        "trade_date": batch.trade_date.isoformat(),
        "observed_at": batch.observed_at.isoformat(),
        "input_hash": batch.input_hash,
        "context_hash": batch.context_hash,
        "production_authority": batch.production_authority,
        "rows": [_row_payload(row) for row in rows],
    }
    encoded = json.dumps(payload, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _row_payload(row: TomorrowStockFeatures) -> dict[str, object]:
    return {
        "code": row.code,
        "board": row.board,
        "industry": row.industry,
        "industry_effective_at": row.industry_effective_at.isoformat(),
        "industry_received_at": row.industry_received_at.isoformat(),
        "as_of": row.as_of.isoformat(),
        "market_cap": row.market_cap,
        "liquidity": row.liquidity,
        "values": tuple((item.name, item.family, item.value) for item in row.values),
        "missing_fields": row.missing_fields,
        "published_facts": tuple(
            {
                "kind": item.kind,
                "name": item.name,
                "value": item.value,
                "report_period": item.report_period.isoformat() if item.report_period is not None else None,
                "published_at": item.published_at.isoformat(),
                "received_at": item.received_at.isoformat(),
                "source": item.source,
                "evidence_hash": item.evidence_hash,
            }
            for item in row.published_facts
        ),
    }


def _context_hash(
    batch: TomorrowFeatureContextBatch,
    contexts: tuple[TomorrowFeatureContext, ...],
) -> str:
    payload = {
        "trade_date": batch.trade_date.isoformat(),
        "input_hash": batch.input_hash,
        "contexts": [
            {
                "code": item.code,
                "board": item.board,
                "industry": item.industry,
                "industry_effective_at": item.industry_effective_at.isoformat(),
                "industry_received_at": item.industry_received_at.isoformat(),
                "observed_at": item.observed_at.isoformat(),
                "current_open": item.current_open,
                "current_high": item.current_high,
                "current_low": item.current_low,
                "current_last": item.current_last,
                "market_cap": item.market_cap,
                "liquidity": item.liquidity,
                "published_facts": tuple(
                    {
                        "kind": fact.kind,
                        "name": fact.name,
                        "value": fact.value,
                        "report_period": fact.report_period.isoformat() if fact.report_period is not None else None,
                        "published_at": fact.published_at.isoformat(),
                        "received_at": fact.received_at.isoformat(),
                        "source": fact.source,
                        "evidence_hash": fact.evidence_hash,
                    }
                    for fact in item.published_facts
                ),
            }
            for item in contexts
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _require_shanghai(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None or getattr(value.tzinfo, "key", None) != _SHANGHAI_TIMEZONE:
        raise ValueError(f"{label} must use Asia/Shanghai")


__all__ = [
    "TOMORROW_FEATURE_SCHEMA_VERSION",
    "TomorrowFeatureContext",
    "TomorrowFeatureContextBatch",
    "TomorrowPointInTimeFeatureBatch",
]

"""Pure unified V2 decision, projection, overlay, and formal-record identities."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal, TypeAlias, cast
from zoneinfo import ZoneInfo

from trader.domain.recommendation.models import RecommendationAction, Strategy

DecisionStage = Literal["local", "hybrid"]
CommitKind = Literal["scheduled", "checkpoint_recovery", "close_fallback"]
DECISION_IDENTITY_SCHEMA_VERSION = "v2_decision_identity_v1"
LONG_PROJECTION_SCHEMA_VERSION = "v2_long_projection_v2"
OVERLAY_SCHEMA_VERSION = "v2_decision_overlay_v1"
COMMITTED_RECORD_SCHEMA_VERSION = "v2_committed_decision_v1"
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_CODE = re.compile(r"^\d{6}$")
_IDENTITY = re.compile(r"^[a-zA-Z0-9_.:+-]{1,160}$")
_REASON = re.compile(r"^[a-z0-9_]{1,64}$")
_Json: TypeAlias = str | int | float | bool | None | list["_Json"] | dict[str, "_Json"]


@dataclass(frozen=True)
class DecisionItem:
    code: str
    action: RecommendationAction
    selected: bool
    rank: int
    candidate_score: float | None
    local_score: float
    final_score: float
    score_components: tuple[tuple[str, float | None], ...]
    risk_codes: tuple[str, ...]
    reason: str

    def __post_init__(self) -> None:
        _require_code(self.code)
        _validate_optional_score(self.candidate_score, "candidate_score")
        _validate_score(self.local_score, "local_score")
        _validate_score(self.final_score, "final_score")
        components = tuple(sorted(self.score_components))
        if not components or len({name for name, _value in components}) != len(components):
            raise ValueError("decision score components must be non-empty and unique")
        for name, value in components:
            _require_identity(name, "score component")
            _validate_optional_score(value, "score component")
        risks = tuple(sorted(set(self.risk_codes)))
        if any(_REASON.fullmatch(value) is None for value in risks):
            raise ValueError("decision risk codes must be structured")
        if _REASON.fullmatch(self.reason) is None:
            raise ValueError("decision reason must be structured")
        if self.selected and (self.rank < 1 or self.action is RecommendationAction.UNAVAILABLE):
            raise ValueError("selected decisions require a positive rank and available action")
        if not self.selected and self.rank != 0:
            raise ValueError("unselected decisions must use rank zero")
        object.__setattr__(self, "score_components", components)
        object.__setattr__(self, "risk_codes", risks)


@dataclass(frozen=True)
class ScoredDecision:
    strategy: Strategy
    trade_date: date
    sequence: int
    observed_at: datetime
    stage: DecisionStage
    parent_version: str | None
    input_versions: tuple[tuple[str, str], ...]
    config_version: str
    strategy_version: str
    fusion_version: str
    items: tuple[DecisionItem, ...]
    filter_aggregates: tuple[tuple[str, int], ...]
    degraded_reasons: tuple[str, ...] = ()
    schema_version: str = DECISION_IDENTITY_SCHEMA_VERSION
    content_hash: str = field(init=False)
    version: str = field(init=False)

    def __post_init__(self) -> None:
        if self.strategy not in {Strategy.TODAY, Strategy.TOMORROW, Strategy.D25}:
            raise ValueError("scored strategy must be today, tomorrow, or d25")
        _validate_coordinates(self.trade_date, self.sequence, self.observed_at)
        if self.schema_version != DECISION_IDENTITY_SCHEMA_VERSION:
            raise ValueError(f"decision schema_version must be {DECISION_IDENTITY_SCHEMA_VERSION}")
        if self.stage == "local" and self.parent_version is not None:
            raise ValueError("local decision cannot reference a parent")
        if self.stage == "hybrid" and not self.parent_version:
            raise ValueError("hybrid decision requires a parent version")
        if self.stage not in {"local", "hybrid"}:
            raise ValueError("decision stage is invalid")
        versions = _normalize_versions(self.input_versions)
        _require_identity(self.config_version, "config version")
        _require_identity(self.strategy_version, "strategy version")
        _require_identity(self.fusion_version, "fusion version")
        items = tuple(sorted(self.items, key=lambda item: item.code))
        if len({item.code for item in items}) != len(items):
            raise ValueError("decision items must contain unique codes")
        selected = sorted((item for item in items if item.selected), key=lambda item: item.rank)
        if [item.rank for item in selected] != list(range(1, len(selected) + 1)):
            raise ValueError("selected decision ranks must be contiguous")
        if self.stage == "local" and any(item.final_score != item.local_score for item in items):
            raise ValueError("local decision final scores must equal local scores")
        aggregates = _normalize_counts(self.filter_aggregates)
        reasons = _normalize_reasons(self.degraded_reasons)
        payload = _scored_payload(self, items, versions, aggregates, reasons)
        content_hash = _hash(payload)
        object.__setattr__(self, "input_versions", versions)
        object.__setattr__(self, "items", items)
        object.__setattr__(self, "filter_aggregates", aggregates)
        object.__setattr__(self, "degraded_reasons", reasons)
        object.__setattr__(self, "content_hash", content_hash)
        object.__setattr__(
            self,
            "version",
            f"decision:{self.strategy.value}:{self.trade_date.isoformat()}:{self.stage}:{self.sequence}:{content_hash[:16]}",
        )


@dataclass(frozen=True)
class LongProjectionItem:
    code: str
    group: str
    quote_version: str
    name: str = ""
    industry: str = ""
    price: float | None = None
    pct_change: float | None = None
    amount: float | None = None
    turnover_rate: float | None = None
    market_cap: float | None = None
    source: str = ""
    source_time: datetime | None = None
    quote_status: Literal["live", "retained", "missing"] = "missing"

    def __post_init__(self) -> None:
        _require_code(self.code)
        _require_identity(self.group, "long group")
        _require_identity(self.quote_version, "long quote version")
        if self.quote_status not in {"live", "retained", "missing"}:
            raise ValueError("long quote status is invalid")
        _validate_optional_market_value(self.price, "long price", positive=True)
        _validate_optional_market_value(self.pct_change, "long pct_change")
        _validate_optional_market_value(self.amount, "long amount", non_negative=True)
        _validate_optional_market_value(self.turnover_rate, "long turnover_rate", non_negative=True)
        _validate_optional_market_value(self.market_cap, "long market_cap", non_negative=True)
        if self.quote_status == "missing":
            if (
                any(
                    value is not None
                    for value in (self.price, self.pct_change, self.amount, self.turnover_rate, self.market_cap)
                )
                or self.source_time is not None
            ):
                raise ValueError("missing long quote cannot contain market values")
        else:
            if not self.name or not self.source or self.source_time is None or self.price is None:
                raise ValueError("available long quote requires name, source, time, and price")
            _require_identity(self.source, "long quote source")
            _require_shanghai(self.source_time, "long quote source_time")


@dataclass(frozen=True)
class LongProjection:
    trade_date: date
    sequence: int
    observed_at: datetime
    input_versions: tuple[tuple[str, str], ...]
    items: tuple[LongProjectionItem, ...]
    schema_version: str = LONG_PROJECTION_SCHEMA_VERSION
    content_hash: str = field(init=False)
    version: str = field(init=False)
    strategy: Strategy = field(init=False, default=Strategy.LONG)

    def __post_init__(self) -> None:
        _validate_coordinates(self.trade_date, self.sequence, self.observed_at)
        if self.schema_version != LONG_PROJECTION_SCHEMA_VERSION:
            raise ValueError(f"long schema_version must be {LONG_PROJECTION_SCHEMA_VERSION}")
        versions = _normalize_versions(self.input_versions)
        items = tuple(self.items)
        if len({item.code for item in items}) != len(items):
            raise ValueError("long projection items must contain unique codes")
        if any(item.source_time is not None and item.source_time > self.observed_at for item in items):
            raise ValueError("long projection cannot contain future quotes")
        payload: dict[str, _Json] = {
            "schema_version": self.schema_version,
            "strategy": self.strategy.value,
            "trade_date": self.trade_date.isoformat(),
            "sequence": self.sequence,
            "observed_at": self.observed_at.isoformat(),
            "input_versions": [[name, version] for name, version in versions],
            "items": [_long_item_payload(item) for item in items],
        }
        content_hash = _hash(payload)
        object.__setattr__(self, "input_versions", versions)
        object.__setattr__(self, "items", items)
        object.__setattr__(self, "content_hash", content_hash)
        object.__setattr__(
            self,
            "version",
            f"projection:long:{self.trade_date.isoformat()}:{self.sequence}:{content_hash[:16]}",
        )


DecisionIdentity: TypeAlias = ScoredDecision | LongProjection


def _long_item_payload(item: LongProjectionItem) -> list[_Json]:
    return [
        item.code,
        item.group,
        item.quote_version,
        item.name,
        item.industry,
        item.price,
        item.pct_change,
        item.amount,
        item.turnover_rate,
        item.market_cap,
        item.source,
        item.source_time.isoformat() if item.source_time is not None else None,
        item.quote_status,
    ]


@dataclass(frozen=True)
class OverlayQuote:
    code: str
    price: float
    pct_change: float | None
    source: str
    source_time: datetime
    data_version: str

    def __post_init__(self) -> None:
        _require_code(self.code)
        if not math.isfinite(self.price) or self.price <= 0.0:
            raise ValueError("overlay price must be finite and positive")
        if self.pct_change is not None and not math.isfinite(self.pct_change):
            raise ValueError("overlay pct_change must be finite")
        _require_identity(self.source, "overlay source")
        _require_identity(self.data_version, "overlay data version")
        _require_shanghai(self.source_time, "overlay source_time")


@dataclass(frozen=True)
class DecisionOverlay:
    strategy: Strategy
    trade_date: date
    parent_version: str
    observed_at: datetime
    quotes: tuple[OverlayQuote, ...]
    schema_version: str = OVERLAY_SCHEMA_VERSION
    content_hash: str = field(init=False)
    version: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != OVERLAY_SCHEMA_VERSION:
            raise ValueError(f"overlay schema_version must be {OVERLAY_SCHEMA_VERSION}")
        if self.strategy not in set(Strategy):
            raise ValueError("overlay strategy is invalid")
        _require_identity(self.parent_version, "overlay parent version")
        _require_shanghai(self.observed_at, "overlay observed_at")
        if self.observed_at.date() != self.trade_date:
            raise ValueError("overlay and observation must share a trade date")
        quotes = tuple(sorted(self.quotes, key=lambda quote: quote.code))
        if len({quote.code for quote in quotes}) != len(quotes):
            raise ValueError("overlay quotes must contain unique codes")
        if any(quote.source_time > self.observed_at for quote in quotes):
            raise ValueError("overlay cannot contain a future quote")
        payload: dict[str, _Json] = {
            "schema_version": self.schema_version,
            "strategy": self.strategy.value,
            "trade_date": self.trade_date.isoformat(),
            "parent_version": self.parent_version,
            "observed_at": self.observed_at.isoformat(),
            "quotes": [_overlay_quote_payload(quote) for quote in quotes],
        }
        content_hash = _hash(payload)
        object.__setattr__(self, "quotes", quotes)
        object.__setattr__(self, "content_hash", content_hash)
        object.__setattr__(
            self,
            "version",
            f"overlay:{self.strategy.value}:{self.trade_date.isoformat()}:{content_hash[:16]}",
        )


@dataclass(frozen=True)
class CommittedDecisionRecord:
    decision: ScoredDecision
    committed_at: datetime
    commit_kind: CommitKind
    schema_version: str = COMMITTED_RECORD_SCHEMA_VERSION
    payload_hash: str = field(init=False)
    version: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != COMMITTED_RECORD_SCHEMA_VERSION:
            raise ValueError(f"record schema_version must be {COMMITTED_RECORD_SCHEMA_VERSION}")
        _require_shanghai(self.committed_at, "decision committed_at")
        if self.committed_at.date() != self.decision.trade_date:
            raise ValueError("formal record and decision must share a trade date")
        if self.committed_at < self.decision.observed_at:
            raise ValueError("formal record cannot predate its decision")
        if self.commit_kind not in {"scheduled", "checkpoint_recovery", "close_fallback"}:
            raise ValueError("formal decision commit kind is invalid")
        payload_hash = _hash(_record_payload(self))
        object.__setattr__(self, "payload_hash", payload_hash)
        object.__setattr__(
            self,
            "version",
            f"record:{self.strategy.value}:{self.trade_date.isoformat()}:{payload_hash[:16]}",
        )

    @property
    def strategy(self) -> Strategy:
        return self.decision.strategy

    @property
    def trade_date(self) -> date:
        return self.decision.trade_date


def identity_codes(identity: DecisionIdentity) -> frozenset[str]:
    if isinstance(identity, ScoredDecision):
        return frozenset(item.code for item in identity.items if item.selected)
    return frozenset(item.code for item in identity.items)


def formal_scored_decision(
    decision: ScoredDecision,
    *,
    degraded_reasons: tuple[str, ...] = (),
    input_versions: tuple[tuple[str, str], ...] = (),
) -> ScoredDecision:
    """Project an accepted scored identity to its immutable official-only form."""

    official_items = tuple(
        item for item in decision.items if item.selected and item.action is RecommendationAction.EXECUTABLE
    )
    return ScoredDecision(
        strategy=decision.strategy,
        trade_date=decision.trade_date,
        sequence=decision.sequence,
        observed_at=decision.observed_at,
        stage=decision.stage,
        parent_version=decision.parent_version,
        input_versions=(*decision.input_versions, *input_versions),
        config_version=decision.config_version,
        strategy_version=decision.strategy_version,
        fusion_version=decision.fusion_version,
        items=official_items,
        filter_aggregates=decision.filter_aggregates,
        degraded_reasons=(*decision.degraded_reasons, *degraded_reasons),
    )


def committed_record_bytes(record: CommittedDecisionRecord) -> bytes:
    payload = _record_payload(record)
    payload["payload_hash"] = record.payload_hash
    payload["version"] = record.version
    return _json_bytes(payload)


def committed_record_from_bytes(payload: bytes) -> CommittedDecisionRecord:
    raw = json.loads(payload.decode("utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("formal decision payload must be an object")
    decision_raw = _object(raw.get("decision"), "decision")
    items = tuple(_decision_item_from_json(item) for item in _list(decision_raw.get("items"), "items"))
    decision = ScoredDecision(
        strategy=Strategy(_text(decision_raw, "strategy")),
        trade_date=date.fromisoformat(_text(decision_raw, "trade_date")),
        sequence=_integer(decision_raw, "sequence"),
        observed_at=_shanghai_datetime(_text(decision_raw, "observed_at")),
        stage=cast(DecisionStage, _text(decision_raw, "stage")),
        parent_version=_optional_text(decision_raw.get("parent_version")),
        input_versions=_pairs(decision_raw.get("input_versions"), "input_versions"),
        config_version=_text(decision_raw, "config_version"),
        strategy_version=_text(decision_raw, "strategy_version"),
        fusion_version=_text(decision_raw, "fusion_version"),
        items=items,
        filter_aggregates=_count_pairs(decision_raw.get("filter_aggregates")),
        degraded_reasons=tuple(_strings(decision_raw.get("degraded_reasons"), "degraded_reasons")),
        schema_version=_text(decision_raw, "schema_version"),
    )
    record = CommittedDecisionRecord(
        decision=decision,
        committed_at=_shanghai_datetime(_text(raw, "committed_at")),
        commit_kind=cast(CommitKind, _text(raw, "commit_kind")),
        schema_version=_text(raw, "schema_version"),
    )
    if raw.get("payload_hash") != record.payload_hash or raw.get("version") != record.version:
        raise ValueError("formal decision payload identity mismatch")
    return record


def _scored_payload(
    decision: ScoredDecision,
    items: tuple[DecisionItem, ...],
    versions: tuple[tuple[str, str], ...],
    aggregates: tuple[tuple[str, int], ...],
    reasons: tuple[str, ...],
) -> dict[str, _Json]:
    return {
        "schema_version": decision.schema_version,
        "strategy": decision.strategy.value,
        "trade_date": decision.trade_date.isoformat(),
        "sequence": decision.sequence,
        "observed_at": decision.observed_at.isoformat(),
        "stage": decision.stage,
        "parent_version": decision.parent_version,
        "input_versions": [[name, version] for name, version in versions],
        "config_version": decision.config_version,
        "strategy_version": decision.strategy_version,
        "fusion_version": decision.fusion_version,
        "items": [_decision_item_payload(item) for item in items],
        "filter_aggregates": [[reason, count] for reason, count in aggregates],
        "degraded_reasons": list(reasons),
    }


def _record_payload(record: CommittedDecisionRecord) -> dict[str, _Json]:
    return {
        "schema_version": record.schema_version,
        "decision": _scored_payload(
            record.decision,
            record.decision.items,
            record.decision.input_versions,
            record.decision.filter_aggregates,
            record.decision.degraded_reasons,
        ),
        "decision_version": record.decision.version,
        "decision_hash": record.decision.content_hash,
        "committed_at": record.committed_at.isoformat(),
        "commit_kind": record.commit_kind,
    }


def _decision_item_payload(item: DecisionItem) -> dict[str, _Json]:
    return {
        "code": item.code,
        "action": item.action.value,
        "selected": item.selected,
        "rank": item.rank,
        "candidate_score": item.candidate_score,
        "local_score": item.local_score,
        "final_score": item.final_score,
        "score_components": [[name, value] for name, value in item.score_components],
        "risk_codes": list(item.risk_codes),
        "reason": item.reason,
    }


def _decision_item_from_json(raw: object) -> DecisionItem:
    value = _object(raw, "decision item")
    return DecisionItem(
        code=_text(value, "code"),
        action=RecommendationAction(_text(value, "action")),
        selected=_boolean(value, "selected"),
        rank=_integer(value, "rank"),
        candidate_score=_optional_number(value.get("candidate_score")),
        local_score=_number(value, "local_score"),
        final_score=_number(value, "final_score"),
        score_components=_score_pairs(value.get("score_components")),
        risk_codes=tuple(_strings(value.get("risk_codes"), "risk_codes")),
        reason=_text(value, "reason"),
    )


def _overlay_quote_payload(quote: OverlayQuote) -> dict[str, _Json]:
    return {
        "code": quote.code,
        "price": quote.price,
        "pct_change": quote.pct_change,
        "source": quote.source,
        "source_time": quote.source_time.isoformat(),
        "data_version": quote.data_version,
    }


def _normalize_versions(values: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
    versions = tuple(sorted(values))
    if not versions or len({name for name, _version in versions}) != len(versions):
        raise ValueError("input versions must be non-empty and unique")
    for name, version in versions:
        _require_identity(name, "input version name")
        _require_identity(version, "input version")
    return versions


def _normalize_counts(values: tuple[tuple[str, int], ...]) -> tuple[tuple[str, int], ...]:
    counts = tuple(sorted(values))
    if len({reason for reason, _count in counts}) != len(counts):
        raise ValueError("filter aggregates must contain unique reasons")
    if any(_REASON.fullmatch(reason) is None or count < 1 for reason, count in counts):
        raise ValueError("filter aggregates must use structured reasons and positive counts")
    return counts


def _normalize_reasons(values: tuple[str, ...]) -> tuple[str, ...]:
    reasons = tuple(sorted(set(values)))
    if any(_REASON.fullmatch(value) is None for value in reasons):
        raise ValueError("degraded reasons must be structured")
    return reasons


def _validate_coordinates(trade_date: date, sequence: int, observed_at: datetime) -> None:
    if sequence < 0:
        raise ValueError("identity sequence cannot be negative")
    _require_shanghai(observed_at, "identity observed_at")
    if observed_at.date() != trade_date:
        raise ValueError("identity observation must match its trade date")


def _validate_score(value: float, label: str) -> None:
    if not math.isfinite(value) or not 0.0 <= value <= 100.0:
        raise ValueError(f"{label} must be finite and in [0, 100]")


def _validate_optional_score(value: float | None, label: str) -> None:
    if value is not None:
        _validate_score(value, label)


def _validate_optional_market_value(
    value: float | None,
    label: str,
    *,
    positive: bool = False,
    non_negative: bool = False,
) -> None:
    if value is None:
        return
    if not math.isfinite(value):
        raise ValueError(f"{label} must be finite")
    if positive and value <= 0.0:
        raise ValueError(f"{label} must be positive")
    if non_negative and value < 0.0:
        raise ValueError(f"{label} cannot be negative")


def _require_code(value: str) -> None:
    if _CODE.fullmatch(value) is None:
        raise ValueError("stock code must contain six digits")


def _require_identity(value: str, label: str) -> None:
    if _IDENTITY.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")


def _require_shanghai(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None or getattr(value.tzinfo, "key", None) != _SHANGHAI.key:
        raise ValueError(f"{label} must use Asia/Shanghai")


def _hash(payload: dict[str, _Json]) -> str:
    return hashlib.sha256(_json_bytes(payload)).hexdigest()


def _json_bytes(payload: dict[str, _Json]) -> bytes:
    return json.dumps(payload, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()


def _object(raw: object, label: str) -> dict[str, object]:
    if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
        raise ValueError(f"{label} must be an object")
    return cast(dict[str, object], raw)


def _list(raw: object, label: str) -> list[object]:
    if not isinstance(raw, list):
        raise ValueError(f"{label} must be a list")
    return raw


def _text(raw: dict[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be non-empty text")
    return value


def _optional_text(raw: object) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw:
        raise ValueError("optional text is invalid")
    return raw


def _integer(raw: dict[str, object], key: str) -> int:
    value = raw.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{key} must be an integer")
    return value


def _boolean(raw: dict[str, object], key: str) -> bool:
    value = raw.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean")
    return value


def _number(raw: dict[str, object], key: str) -> float:
    value = raw.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{key} must be a number")
    return float(value)


def _optional_number(raw: object) -> float | None:
    if raw is None:
        return None
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        raise ValueError("optional number is invalid")
    return float(raw)


def _strings(raw: object, label: str) -> list[str]:
    values = _list(raw, label)
    if any(not isinstance(value, str) for value in values):
        raise ValueError(f"{label} must contain text")
    return cast(list[str], values)


def _pairs(raw: object, label: str) -> tuple[tuple[str, str], ...]:
    pairs: list[tuple[str, str]] = []
    for item in _list(raw, label):
        values = _list(item, label)
        if len(values) != 2 or any(not isinstance(value, str) for value in values):
            raise ValueError(f"{label} entries are invalid")
        pairs.append((cast(str, values[0]), cast(str, values[1])))
    return tuple(pairs)


def _count_pairs(raw: object) -> tuple[tuple[str, int], ...]:
    pairs: list[tuple[str, int]] = []
    for item in _list(raw, "filter_aggregates"):
        values = _list(item, "filter aggregate")
        if (
            len(values) != 2
            or not isinstance(values[0], str)
            or not isinstance(values[1], int)
            or isinstance(values[1], bool)
        ):
            raise ValueError("filter aggregate entries are invalid")
        pairs.append((values[0], values[1]))
    return tuple(pairs)


def _score_pairs(raw: object) -> tuple[tuple[str, float | None], ...]:
    pairs: list[tuple[str, float | None]] = []
    for item in _list(raw, "score_components"):
        values = _list(item, "score component")
        if len(values) != 2 or not isinstance(values[0], str):
            raise ValueError("score component entries are invalid")
        pairs.append((values[0], _optional_number(values[1])))
    return tuple(pairs)


def _shanghai_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("persisted datetime must be timezone-aware")
    return parsed.astimezone(_SHANGHAI)


__all__ = [
    "COMMITTED_RECORD_SCHEMA_VERSION",
    "DECISION_IDENTITY_SCHEMA_VERSION",
    "LONG_PROJECTION_SCHEMA_VERSION",
    "OVERLAY_SCHEMA_VERSION",
    "CommitKind",
    "CommittedDecisionRecord",
    "DecisionIdentity",
    "DecisionItem",
    "DecisionOverlay",
    "DecisionStage",
    "LongProjection",
    "LongProjectionItem",
    "OverlayQuote",
    "ScoredDecision",
    "committed_record_bytes",
    "committed_record_from_bytes",
    "formal_scored_decision",
    "identity_codes",
]

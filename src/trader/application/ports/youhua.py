"""youhua phase-2 public contracts owned by the application layer."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Literal

from trader.application.ports.types import JsonObject, freeze_json_object
from trader.domain.market.models import FeatureSnapshot, LiveQuote
from trader.domain.recommendation.models import FusionMode, Recommendation, Strategy

CONTRACT_VERSION = "youhua_contract_base_v1"
P3_P4_SCHEMA_VERSION = "p3_p4_feature_snapshot_market_change_set_v1"
MARKET_CHANGE_SET_VERSION = "market_change_set_v1"
P4_P5_SCHEMA_VERSION = "p4_p5_high_value_review_manifest_v1"
DEEPSEEK_V4_FACTS_VERSION = "deepseek_v4_review_facts_v1"
EVIDENCE_MANIFEST_VERSION = "evidence_manifest_v1"
REVIEW_OWNER_IDENTITY_VERSION = "review_owner_strategy_identity_v1"
P6_PROJECTION_EVENT_VERSION = "p4p5_p6_projection_event_v1"
P6_OVERLAY_EVENT_VERSION = "p6_overlay_event_v1"
P6_RESYNC_REASON_VERSION = "p6_resync_reason_v1"

LOGICAL_CACHE_LIMIT_BYTES = 248 * 1024 * 1024
PROCESS_PEAK_RSS_LIMIT_BYTES = 384 * 1024 * 1024


class YouhuaContractError(ValueError):
    """A public youhua contract value failed validation."""


class ResyncReason(str, Enum):
    CURSOR_EXPIRED = "cursor_expired"
    CURSOR_AHEAD = "cursor_ahead"
    CURSOR_GAP = "cursor_gap"
    SLOW_SUBSCRIBER = "slow_subscriber"
    BASE_MISMATCH = "base_mismatch"
    SCHEMA_MISMATCH = "schema_mismatch"
    IDENTITY_MISMATCH = "identity_mismatch"


@dataclass(frozen=True)
class MemoryBudgetContract:
    contract_version: str
    cache_logical_bytes: int
    process_peak_rss_bytes: int

    def __post_init__(self) -> None:
        _require_version(self.contract_version, CONTRACT_VERSION, "memory contract")
        if self.cache_logical_bytes != LOGICAL_CACHE_LIMIT_BYTES:
            raise YouhuaContractError("logical cache budget must be exactly 248 MiB")
        if self.process_peak_rss_bytes != PROCESS_PEAK_RSS_LIMIT_BYTES:
            raise YouhuaContractError("process peak RSS budget must be exactly 384 MiB")
        if self.process_peak_rss_bytes <= self.cache_logical_bytes:
            raise YouhuaContractError("process peak RSS must be separate from and larger than logical cache")

    def to_status(self) -> JsonObject:
        return freeze_json_object(
            {
                "contract_version": self.contract_version,
                "cache_logical_bytes": self.cache_logical_bytes,
                "process_peak_rss_bytes": self.process_peak_rss_bytes,
            }
        )


@dataclass(frozen=True)
class MemoryUsageSnapshot:
    cache_logical_bytes: int
    process_peak_rss_bytes: int

    def __post_init__(self) -> None:
        if self.cache_logical_bytes < 0 or self.process_peak_rss_bytes < 0:
            raise YouhuaContractError("memory usage counters cannot be negative")


@dataclass(frozen=True)
class MemoryActivationDecision:
    allowed: bool
    rejected_reason: str
    contract: MemoryBudgetContract
    usage: MemoryUsageSnapshot

    def to_status(self) -> JsonObject:
        return freeze_json_object(
            {
                "allowed": self.allowed,
                "rejected_reason": self.rejected_reason,
                "contract": self.contract.to_status(),
                "usage": {
                    "cache_logical_bytes": self.usage.cache_logical_bytes,
                    "process_peak_rss_bytes": self.usage.process_peak_rss_bytes,
                },
            }
        )


@dataclass(frozen=True)
class MarketChangeSet:
    schema_version: str
    merge_epoch: str
    previous_merge_epoch: str | None
    inserted_codes: tuple[str, ...] = ()
    updated_codes: tuple[str, ...] = ()
    removed_codes: tuple[str, ...] = ()
    dirty_codes: tuple[str, ...] = ()
    dirty_boards: tuple[str, ...] = ()
    dirty_industries: tuple[str, ...] = ()
    dirty_field_families: tuple[str, ...] = ()
    overlay_only: bool = False
    full_invalidation_reason: str | None = None
    evidence_manifest_hash: str = ""
    content_hash: str = ""

    def __post_init__(self) -> None:
        _require_version(self.schema_version, MARKET_CHANGE_SET_VERSION, "market change set")
        _require_text(self.merge_epoch, "merge_epoch")
        _require_text(self.evidence_manifest_hash, "evidence_manifest_hash")
        _require_hash_text(self.content_hash, "content_hash")
        for field_name in ("inserted_codes", "updated_codes", "removed_codes", "dirty_codes"):
            codes = _sorted_unique_codes(getattr(self, field_name), field_name)
            object.__setattr__(self, field_name, codes)
        for field_name in ("dirty_boards", "dirty_industries", "dirty_field_families"):
            values = _sorted_unique_texts(getattr(self, field_name), field_name)
            object.__setattr__(self, field_name, values)
        if self.overlay_only and any((self.inserted_codes, self.updated_codes, self.removed_codes)):
            raise YouhuaContractError("overlay-only change set cannot alter feature rows")
        if self.full_invalidation_reason is not None and not self.full_invalidation_reason.strip():
            raise YouhuaContractError("full invalidation reason cannot be blank")

    @property
    def is_dirty(self) -> bool:
        return bool(
            self.dirty_codes
            or self.dirty_boards
            or self.dirty_industries
            or self.dirty_field_families
            or self.full_invalidation_reason
        )


@dataclass(frozen=True)
class FeatureSnapshotEnvelope:
    schema_version: str
    snapshot_version: str
    feature_snapshot_version: str
    trade_date: str
    phase: str
    merge_epoch: str
    data_version: str
    config_version: str
    feature_schema_version: str
    content_hash: str
    feature_snapshots: tuple[FeatureSnapshot, ...]
    market_change_set: MarketChangeSet

    def __post_init__(self) -> None:
        _require_version(self.schema_version, P3_P4_SCHEMA_VERSION, "P3 -> P4 envelope")
        for field_name in (
            "snapshot_version",
            "feature_snapshot_version",
            "trade_date",
            "phase",
            "merge_epoch",
            "data_version",
            "config_version",
            "feature_schema_version",
            "content_hash",
        ):
            _require_text(getattr(self, field_name), field_name)
        if self.merge_epoch != self.market_change_set.merge_epoch:
            raise YouhuaContractError("feature envelope and market change set merge_epoch must match")
        codes = tuple(feature.quote.code for feature in self.feature_snapshots)
        if codes != tuple(sorted(codes)) or len(set(codes)) != len(codes):
            raise YouhuaContractError("feature snapshots must be sorted by unique code")


@dataclass(frozen=True)
class EvidenceManifestItem:
    evidence_id: str
    evidence_type: str
    source_tier: str
    source: str
    published_at: datetime
    received_at: datetime
    data_version: str
    event_key: str
    supports_positive_fact: bool
    counter_evidence: bool
    price_reaction_bucket: str

    def __post_init__(self) -> None:
        for field_name in (
            "evidence_id",
            "evidence_type",
            "source_tier",
            "source",
            "data_version",
            "event_key",
            "price_reaction_bucket",
        ):
            _require_text(getattr(self, field_name), field_name)
        _require_aware(self.published_at, "published_at")
        _require_aware(self.received_at, "received_at")
        if self.published_at > self.received_at:
            raise YouhuaContractError("evidence published_at cannot be after received_at")


@dataclass(frozen=True)
class EvidenceManifest:
    schema_version: str
    manifest_hash: str
    items: tuple[EvidenceManifestItem, ...] = ()

    def __post_init__(self) -> None:
        _require_version(self.schema_version, EVIDENCE_MANIFEST_VERSION, "evidence manifest")
        _require_hash_text(self.manifest_hash, "manifest_hash")
        evidence_ids = tuple(item.evidence_id for item in self.items)
        if len(set(evidence_ids)) != len(evidence_ids):
            raise YouhuaContractError("evidence manifest ids must be unique")
        object.__setattr__(self, "items", tuple(sorted(self.items, key=lambda item: item.evidence_id)))

    def contains_all(self, evidence_ids: tuple[str, ...]) -> bool:
        available = {item.evidence_id for item in self.items}
        return all(evidence_id in available for evidence_id in evidence_ids)


@dataclass(frozen=True)
class ReviewOwnerIdentity:
    schema_version: str
    owner_strategy: Strategy
    consumer_strategy: Strategy
    generation: str
    budget_bucket: str
    model_role: str
    model: str
    thinking_mode: str
    prompt_version: str
    facts_schema_version: str
    config_version: str

    def __post_init__(self) -> None:
        _require_version(self.schema_version, REVIEW_OWNER_IDENTITY_VERSION, "review owner identity")
        for field_name in (
            "generation",
            "budget_bucket",
            "model_role",
            "model",
            "thinking_mode",
            "prompt_version",
            "facts_schema_version",
            "config_version",
        ):
            _require_text(getattr(self, field_name), field_name)


@dataclass(frozen=True)
class HighValueReviewInput:
    contract_version: str
    strategy: Strategy
    trade_date: str
    phase: str
    deadline: datetime
    owner_identity: ReviewOwnerIdentity
    candidate_code: str
    feature_snapshot_identity: str
    local_score: float
    local_rank: int
    action_threshold: float | None
    in_protection_set: bool
    near_action_threshold: bool
    near_global_boundary: bool
    topk_boundary: bool
    has_new_high_risk: bool
    has_new_catalyst: bool
    direction_conflict: bool
    evidence_conflict: bool
    was_reviewed: bool
    evidence_manifest_hash: str
    price_reaction_bucket: str
    budget_bucket: str

    def __post_init__(self) -> None:
        _require_version(self.contract_version, P4_P5_SCHEMA_VERSION, "high-value review input")
        _require_text(self.trade_date, "trade_date")
        _require_text(self.phase, "phase")
        _require_aware(self.deadline, "deadline")
        _require_code(self.candidate_code, "candidate_code")
        _require_hash_text(self.feature_snapshot_identity, "feature_snapshot_identity")
        _require_hash_text(self.evidence_manifest_hash, "evidence_manifest_hash")
        _require_text(self.price_reaction_bucket, "price_reaction_bucket")
        _require_text(self.budget_bucket, "budget_bucket")
        if not math.isfinite(self.local_score):
            raise YouhuaContractError("local_score must be finite")
        if self.local_rank < 1:
            raise YouhuaContractError("local_rank must be positive")
        if self.action_threshold is not None and not math.isfinite(self.action_threshold):
            raise YouhuaContractError("action_threshold must be finite when present")
        if self.owner_identity.owner_strategy is not self.strategy:
            raise YouhuaContractError("owner strategy must match review input strategy")


@dataclass(frozen=True)
class HighValueReviewManifest:
    schema_version: str
    strategy: Strategy
    trade_date: str
    phase: str
    evidence_manifest: EvidenceManifest
    inputs: tuple[HighValueReviewInput, ...] = ()

    def __post_init__(self) -> None:
        _require_version(self.schema_version, P4_P5_SCHEMA_VERSION, "high-value review manifest")
        _require_text(self.trade_date, "trade_date")
        _require_text(self.phase, "phase")
        if self.strategy is Strategy.LONG and self.inputs:
            raise YouhuaContractError("long review input collection must be empty")
        codes = tuple(item.candidate_code for item in self.inputs)
        if len(set(codes)) != len(codes):
            raise YouhuaContractError("review input candidate codes must be unique")
        if any(item.strategy is not self.strategy or item.trade_date != self.trade_date for item in self.inputs):
            raise YouhuaContractError("review input identities must match the manifest")
        if any(item.evidence_manifest_hash != self.evidence_manifest.manifest_hash for item in self.inputs):
            raise YouhuaContractError("review inputs must reference the manifest hash")
        object.__setattr__(self, "inputs", tuple(sorted(self.inputs, key=lambda item: item.candidate_code)))


@dataclass(frozen=True)
class DirectionalFact:
    direction: Literal["positive", "neutral", "negative", "unknown"]
    importance: Literal["low", "medium", "high", "unknown"] = "unknown"
    confirmation: Literal["official", "multi_source", "single_source", "unsupported"] = "unsupported"
    cycle: str = "unknown"
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_ids", tuple(sorted(set(self.evidence_ids))))


@dataclass(frozen=True)
class PriceReactionFact:
    bucket: str
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.bucket, "price reaction bucket")
        object.__setattr__(self, "evidence_ids", tuple(sorted(set(self.evidence_ids))))


@dataclass(frozen=True)
class RiskFactFlags:
    regulatory: bool = False
    shareholder_reduction: bool = False
    unlock: bool = False
    pledge: bool = False
    litigation: bool = False
    earnings: bool = False


@dataclass(frozen=True)
class DeepSeekV4Facts:
    contract_version: str
    code: str
    abstain: bool
    catalyst: DirectionalFact
    price_reaction: PriceReactionFact
    fundamental: DirectionalFact
    industry_policy: DirectionalFact
    risks: RiskFactFlags
    conflicts: tuple[str, ...] = ()
    coverage: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_version(self.contract_version, DEEPSEEK_V4_FACTS_VERSION, "DeepSeek facts")
        _require_code(self.code, "code")
        object.__setattr__(self, "conflicts", tuple(sorted(set(self.conflicts))))
        object.__setattr__(self, "coverage", tuple(sorted(set(self.coverage))))

    def validate_against_manifest(self, manifest: EvidenceManifest) -> None:
        evidence_ids = (
            self.catalyst.evidence_ids
            + self.price_reaction.evidence_ids
            + self.fundamental.evidence_ids
            + self.industry_policy.evidence_ids
        )
        if not manifest.contains_all(evidence_ids):
            raise YouhuaContractError("DeepSeek facts reference evidence outside the manifest")


@dataclass(frozen=True)
class ProjectionUpsert:
    code: str
    rank: int
    action: str
    score: float
    recommendation: Recommendation | None = None

    def __post_init__(self) -> None:
        _require_code(self.code, "projection upsert code")
        _require_text(self.action, "projection upsert action")
        if self.rank < 1:
            raise YouhuaContractError("projection rank must be positive")
        if not math.isfinite(self.score):
            raise YouhuaContractError("projection score must be finite")


@dataclass(frozen=True)
class ProjectionEvent:
    schema_version: str
    event_id: str
    projection_version: str
    base_projection_version: str | None
    etag: str
    snapshot_id: str
    strategy: Strategy
    trade_date: str
    view: Literal["official", "live", "long"]
    phase: str
    published_at: datetime
    strategy_version: str
    fusion_mode: FusionMode
    stale: bool
    frozen: bool
    degraded_reasons: tuple[str, ...]
    filtered_count: int
    upserts: tuple[ProjectionUpsert, ...] = ()
    removed_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_version(self.schema_version, P6_PROJECTION_EVENT_VERSION, "P6 projection event")
        for field_name in (
            "event_id",
            "projection_version",
            "etag",
            "snapshot_id",
            "trade_date",
            "phase",
            "strategy_version",
        ):
            _require_text(getattr(self, field_name), field_name)
        _require_aware(self.published_at, "published_at")
        if self.filtered_count < 0:
            raise YouhuaContractError("filtered_count cannot be negative")
        removed_codes = _sorted_unique_codes(self.removed_codes, "removed_codes")
        upsert_codes = tuple(item.code for item in self.upserts)
        if len(set(upsert_codes)) != len(upsert_codes):
            raise YouhuaContractError("projection upsert codes must be unique")
        if set(upsert_codes).intersection(removed_codes):
            raise YouhuaContractError("projection cannot upsert and remove the same code")
        object.__setattr__(self, "removed_codes", removed_codes)
        object.__setattr__(self, "upserts", tuple(sorted(self.upserts, key=lambda item: item.code)))
        object.__setattr__(self, "degraded_reasons", tuple(sorted(set(self.degraded_reasons))))


@dataclass(frozen=True)
class OverlayQuote:
    code: str
    price: float | None
    pct_change: float | None
    source: str
    source_time: datetime
    quote_data_version: str
    data_age_seconds: float | None = None

    def __post_init__(self) -> None:
        _require_code(self.code, "overlay quote code")
        _require_text(self.source, "overlay quote source")
        _require_aware(self.source_time, "overlay quote source_time")
        _require_text(self.quote_data_version, "quote_data_version")
        for field_name in ("price", "pct_change", "data_age_seconds"):
            value = getattr(self, field_name)
            if value is not None and not math.isfinite(value):
                raise YouhuaContractError(f"{field_name} must be finite when present")

    @classmethod
    def from_live_quote(cls, quote: LiveQuote, *, observed_at: datetime) -> OverlayQuote:
        return cls(
            code=quote.code,
            price=quote.price,
            pct_change=quote.pct_change,
            source=quote.source,
            source_time=quote.source_time,
            quote_data_version=quote.data_version,
            data_age_seconds=quote.age_seconds(observed_at),
        )


@dataclass(frozen=True)
class OverlayEvent:
    schema_version: str
    event_id: str
    projection_version: str
    overlay_version: str
    snapshot_id: str
    strategy: Strategy
    trade_date: str
    observed_at: datetime
    closing: bool
    quotes: tuple[OverlayQuote, ...] = ()

    def __post_init__(self) -> None:
        _require_version(self.schema_version, P6_OVERLAY_EVENT_VERSION, "P6 overlay event")
        for field_name in ("event_id", "projection_version", "overlay_version", "snapshot_id", "trade_date"):
            _require_text(getattr(self, field_name), field_name)
        _require_aware(self.observed_at, "observed_at")
        codes = tuple(quote.code for quote in self.quotes)
        if len(set(codes)) != len(codes):
            raise YouhuaContractError("overlay quote codes must be unique")
        if any(quote.source_time > self.observed_at for quote in self.quotes):
            raise YouhuaContractError("overlay cannot contain future quotes")
        object.__setattr__(self, "quotes", tuple(sorted(self.quotes, key=lambda item: item.code)))


def default_memory_budget_contract() -> MemoryBudgetContract:
    return MemoryBudgetContract(
        contract_version=CONTRACT_VERSION,
        cache_logical_bytes=LOGICAL_CACHE_LIMIT_BYTES,
        process_peak_rss_bytes=PROCESS_PEAK_RSS_LIMIT_BYTES,
    )


def validate_memory_activation(
    usage: MemoryUsageSnapshot,
    *,
    contract: MemoryBudgetContract | None = None,
) -> MemoryActivationDecision:
    active_contract = contract or default_memory_budget_contract()
    if usage.cache_logical_bytes > active_contract.cache_logical_bytes:
        return MemoryActivationDecision(False, "cache_logical_bytes_exceeded", active_contract, usage)
    if usage.process_peak_rss_bytes > active_contract.process_peak_rss_bytes:
        return MemoryActivationDecision(False, "process_peak_rss_bytes_exceeded", active_contract, usage)
    return MemoryActivationDecision(True, "", active_contract, usage)


def public_schema_versions() -> JsonObject:
    return freeze_json_object(
        {
            "contract": CONTRACT_VERSION,
            "p3_p4": P3_P4_SCHEMA_VERSION,
            "market_change_set": MARKET_CHANGE_SET_VERSION,
            "p4_p5": P4_P5_SCHEMA_VERSION,
            "deepseek_facts": DEEPSEEK_V4_FACTS_VERSION,
            "evidence_manifest": EVIDENCE_MANIFEST_VERSION,
            "review_owner_identity": REVIEW_OWNER_IDENTITY_VERSION,
            "p6_projection": P6_PROJECTION_EVENT_VERSION,
            "p6_overlay": P6_OVERLAY_EVENT_VERSION,
            "p6_resync_reason": P6_RESYNC_REASON_VERSION,
        }
    )


def _require_version(actual: str, expected: str, label: str) -> None:
    if actual != expected:
        raise YouhuaContractError(f"{label} schema version must be {expected}")


def _require_text(value: str, label: str) -> None:
    if not value.strip():
        raise YouhuaContractError(f"{label} must not be empty")


def _require_hash_text(value: str, label: str) -> None:
    _require_text(value, label)


def _require_code(value: str, label: str) -> None:
    if len(value) != 6 or not value.isdigit():
        raise YouhuaContractError(f"{label} must be a normalized six-digit stock code")


def _require_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise YouhuaContractError(f"{label} must be timezone-aware")


def _sorted_unique_codes(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    if any(len(value) != 6 or not value.isdigit() for value in values):
        raise YouhuaContractError(f"{label} must contain normalized six-digit stock codes")
    if len(set(values)) != len(values):
        raise YouhuaContractError(f"{label} must be unique")
    return tuple(sorted(values))


def _sorted_unique_texts(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    if any(not value.strip() for value in values):
        raise YouhuaContractError(f"{label} cannot contain blank values")
    if len(set(values)) != len(values):
        raise YouhuaContractError(f"{label} must be unique")
    return tuple(sorted(values))


__all__ = [
    "CONTRACT_VERSION",
    "DEEPSEEK_V4_FACTS_VERSION",
    "EVIDENCE_MANIFEST_VERSION",
    "LOGICAL_CACHE_LIMIT_BYTES",
    "MARKET_CHANGE_SET_VERSION",
    "P3_P4_SCHEMA_VERSION",
    "P4_P5_SCHEMA_VERSION",
    "P6_OVERLAY_EVENT_VERSION",
    "P6_PROJECTION_EVENT_VERSION",
    "P6_RESYNC_REASON_VERSION",
    "PROCESS_PEAK_RSS_LIMIT_BYTES",
    "REVIEW_OWNER_IDENTITY_VERSION",
    "DeepSeekV4Facts",
    "DirectionalFact",
    "EvidenceManifest",
    "EvidenceManifestItem",
    "FeatureSnapshotEnvelope",
    "HighValueReviewInput",
    "HighValueReviewManifest",
    "MarketChangeSet",
    "MemoryActivationDecision",
    "MemoryBudgetContract",
    "MemoryUsageSnapshot",
    "OverlayEvent",
    "OverlayQuote",
    "PriceReactionFact",
    "ProjectionEvent",
    "ProjectionUpsert",
    "ResyncReason",
    "ReviewOwnerIdentity",
    "RiskFactFlags",
    "YouhuaContractError",
    "default_memory_budget_contract",
    "public_schema_versions",
    "validate_memory_activation",
]

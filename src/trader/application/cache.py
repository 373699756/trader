"""Stable cache identities and application-owned cache contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from functools import lru_cache
from types import MappingProxyType
from typing import Any, Generic, Protocol, TypeVar, cast

_T = TypeVar("_T")

SLOW_DATASETS = frozenset(
    {
        "daily_history",
        "security_master_calendar",
        "daily_valuation_financials",
    }
)


@dataclass(frozen=True)
class CacheDatasetPolicy:
    refresh_ttl_seconds: float | None
    action_max_age_seconds: float | None
    cadence_task: str | None
    action_max_age_multiplier: float | None
    negative_ttl_seconds: float
    capacity: int
    group: str
    persisted: bool

    def __post_init__(self) -> None:
        if self.refresh_ttl_seconds is not None and self.refresh_ttl_seconds <= 0:
            raise ValueError("cache refresh TTL must be positive")
        if self.action_max_age_seconds is not None and self.action_max_age_seconds <= 0:
            raise ValueError("cache action age must be positive")
        if self.action_max_age_seconds is not None and self.refresh_ttl_seconds is not None:
            if self.action_max_age_seconds < self.refresh_ttl_seconds:
                raise ValueError("cache action age cannot be smaller than refresh TTL")
        cadence_fields = (self.cadence_task, self.action_max_age_multiplier)
        if (cadence_fields[0] is None) != (cadence_fields[1] is None):
            raise ValueError("cadence cache policy must define both cadence fields")
        if self.cadence_task is None and self.refresh_ttl_seconds is None:
            raise ValueError("non-cadence cache policy requires refresh TTL")
        if self.cadence_task is None and self.action_max_age_seconds is None:
            raise ValueError("non-cadence cache policy requires action age")
        if self.cadence_task is not None and self.refresh_ttl_seconds is not None:
            raise ValueError("cadence cache policy cannot define a fixed refresh TTL")
        if self.cadence_task is not None and self.action_max_age_seconds is not None:
            raise ValueError("cadence cache policy cannot define a fixed action age")
        if self.action_max_age_multiplier is not None and self.action_max_age_multiplier < 1:
            raise ValueError("cache action age multiplier must be at least one")
        if self.negative_ttl_seconds <= 0:
            raise ValueError("cache negative TTL must be positive")
        if self.capacity <= 0:
            raise ValueError("cache capacity must be positive")
        if not self.group:
            raise ValueError("cache group must not be empty")


@dataclass(frozen=True)
class CacheGroupPolicy:
    max_bytes: int

    def __post_init__(self) -> None:
        if self.max_bytes <= 0:
            raise ValueError("cache group byte limit must be positive")


@dataclass(frozen=True)
class CachePolicy:
    schema_version: int
    policy_version: str
    datasets: Mapping[str, CacheDatasetPolicy]
    groups: Mapping[str, CacheGroupPolicy]
    total_bytes: int
    runtime_reserve_bytes: int
    pool_total_bytes: int
    estimator_version: str

    def __post_init__(self) -> None:
        if self.schema_version != 6:
            raise ValueError("cache schema version must be 6")
        if not self.policy_version or not self.estimator_version:
            raise ValueError("cache policy and estimator versions must not be empty")
        dataset_copy = dict(self.datasets)
        group_copy = dict(self.groups)
        if not dataset_copy or not group_copy:
            raise ValueError("cache policy requires datasets and groups")
        if any(policy.group not in group_copy for policy in dataset_copy.values()):
            raise ValueError("cache dataset references an unknown group")
        if self.total_bytes <= 0 or sum(group.max_bytes for group in group_copy.values()) != self.total_bytes:
            raise ValueError("cache group byte limits must sum to total_bytes")
        if self.runtime_reserve_bytes <= 0 or self.pool_total_bytes != self.total_bytes + self.runtime_reserve_bytes:
            raise ValueError("cache pools and runtime reserve must sum to pool_total_bytes")
        object.__setattr__(self, "datasets", MappingProxyType(dataset_copy))
        object.__setattr__(self, "groups", MappingProxyType(group_copy))


@dataclass(frozen=True, order=True)
class CacheIdentity:
    dataset: str
    source: str
    subject_key: str
    request_fingerprint: str
    trade_date: str
    phase: str
    source_contract_version: str
    config_version: str
    schema_version: str

    def __post_init__(self) -> None:
        values = (
            self.dataset,
            self.source,
            self.subject_key,
            self.request_fingerprint,
            self.trade_date,
            self.phase,
            self.source_contract_version,
            self.config_version,
            self.schema_version,
        )
        if any(not value.strip() for value in values):
            raise ValueError("cache identity fields must not be empty")
        if len(self.request_fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in self.request_fingerprint
        ):
            raise ValueError("cache request fingerprint must be a lowercase SHA-256")

    def as_dict(self) -> dict[str, str]:
        return {
            "dataset": self.dataset,
            "source": self.source,
            "subject_key": self.subject_key,
            "request_fingerprint": self.request_fingerprint,
            "trade_date": self.trade_date,
            "phase": self.phase,
            "source_contract_version": self.source_contract_version,
            "config_version": self.config_version,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class CacheStats:
    entries: int
    capacity: int
    hit: int
    miss: int
    refresh_due_hit: int
    stale_hit: int
    degraded_hit: int
    negative_hit: int
    refresh: int
    eviction: int
    load_error: int
    estimated_bytes: int

    @property
    def hit_rate(self) -> float:
        total = self.hit + self.miss
        return self.hit / total if total else 0.0


@dataclass(frozen=True)
class CacheLookup(Generic[_T]):
    value: _T | None
    state: str
    data_version: str | None
    source_time: datetime | None
    error_code: str | None = None
    retry_suppressed: bool = False


class BoundedCache(Protocol, Generic[_T]):
    def get(self, identity: CacheIdentity) -> CacheLookup[_T] | None: ...

    def put(
        self,
        identity: CacheIdentity,
        value: _T,
        *,
        data_version: str,
        source_time: datetime,
    ) -> bool: ...

    def put_negative(self, identity: CacheIdentity, *, error_code: str) -> bool: ...

    def coalesce(self, identity: CacheIdentity, loader: Callable[[], _T]) -> _T: ...

    def is_actionable(self, identity: CacheIdentity, source_time: datetime) -> bool: ...

    def status(self) -> Mapping[str, Mapping[str, Mapping[str, object]]]: ...

    def stop(self, *, wait: bool = True, timeout_seconds: float | None = None) -> None: ...


def build_cache_identity(
    *,
    dataset: str,
    source: str,
    subject_key: str,
    request: Mapping[str, object],
    trade_date: str,
    phase: str,
    source_contract_version: str,
    config_version: str,
    schema_version: str,
) -> CacheIdentity:
    normalized_phase = "all_day" if dataset in SLOW_DATASETS else normalize_cache_phase(phase)
    return CacheIdentity(
        dataset=dataset,
        source=source,
        subject_key=subject_key,
        request_fingerprint=request_fingerprint(request),
        trade_date=trade_date,
        phase=normalized_phase,
        source_contract_version=source_contract_version,
        config_version=config_version,
        schema_version=schema_version,
    )


def normalize_cache_phase(phase: str) -> str:
    normalized = phase.strip().lower()
    aliases = {
        "today_observe": "today_main",
        "deepseek_cutoff": "final_window",
        "final_quote": "final_window",
        "frozen": "final_window",
    }
    return aliases.get(normalized, normalized)


def request_fingerprint(request: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_json_bytes(_normalize_request(request))).hexdigest()


def _normalize_request(request: Mapping[str, object]) -> dict[str, object]:
    normalized: dict[str, object] = {}
    for key, value in request.items():
        if key in {"codes", "fields"} and isinstance(value, (tuple, list, set, frozenset)):
            normalized[key] = sorted({str(item) for item in value})
        elif isinstance(value, Mapping):
            normalized[key] = _normalize_request(value)
        else:
            normalized[key] = value
    return normalized


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        cls=_CanonicalJsonEncoder,
    ).encode("utf-8")


class _CanonicalJsonEncoder(json.JSONEncoder):
    def default(self, value: object) -> object:
        if isinstance(value, Decimal):
            if not value.is_finite():
                raise ValueError("canonical JSON decimals must be finite")
            return format(value, "f")
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("canonical JSON datetime must be timezone-aware")
            return value.isoformat()
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, Mapping):
            return {str(key): item for key, item in value.items()}
        if isinstance(value, (set, frozenset)):
            return sorted(value, key=canonical_json_bytes)
        if is_dataclass(value) and not isinstance(value, type):
            return {name: getattr(value, name) for name in _canonical_field_names(type(value))}
        raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


@lru_cache(maxsize=32)
def _canonical_field_names(value_type: type[Any]) -> tuple[str, ...]:
    return tuple(field.name for field in fields(value_type))


def freeze_cache_value(value: _T) -> _T:
    if isinstance(value, Mapping):
        frozen = MappingProxyType({str(key): freeze_cache_value(item) for key, item in value.items()})
        return cast(_T, frozen)
    if isinstance(value, list):
        return cast(_T, tuple(freeze_cache_value(item) for item in value))
    if isinstance(value, tuple):
        return cast(_T, tuple(freeze_cache_value(item) for item in value))
    if isinstance(value, set):
        return cast(_T, frozenset(freeze_cache_value(item) for item in value))
    return value


__all__ = [
    "BoundedCache",
    "CacheDatasetPolicy",
    "CacheGroupPolicy",
    "CacheIdentity",
    "CacheLookup",
    "CachePolicy",
    "CacheStats",
    "SLOW_DATASETS",
    "build_cache_identity",
    "canonical_json_bytes",
    "freeze_cache_value",
    "normalize_cache_phase",
    "request_fingerprint",
]

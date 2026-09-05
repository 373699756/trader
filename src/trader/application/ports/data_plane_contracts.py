"""Small typed contracts shared by the market data plane."""

from __future__ import annotations

from dataclasses import dataclass

from trader.domain.market.models import FeatureSnapshot

LOGICAL_CACHE_LIMIT_BYTES = 248 * 1024 * 1024
PROCESS_PEAK_RSS_LIMIT_BYTES = 384 * 1024 * 1024
MARKET_CHANGE_SET_VERSION = "market_change_set"
FEATURE_ENVELOPE_VERSION = "feature_snapshot_envelope"


class DataPlaneContractError(ValueError):
    """A unified data-plane value failed validation."""


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
        if self.schema_version != MARKET_CHANGE_SET_VERSION:
            raise DataPlaneContractError("market change set schema version is invalid")
        _require_text(self.merge_epoch, "merge_epoch")
        for field_name in ("inserted_codes", "updated_codes", "removed_codes", "dirty_codes"):
            values = tuple(getattr(self, field_name))
            if len(values) != len(set(values)) or values != tuple(sorted(values)):
                raise DataPlaneContractError(f"{field_name} must be sorted and unique")
            object.__setattr__(self, field_name, values)
        if self.overlay_only and any((self.inserted_codes, self.updated_codes, self.removed_codes)):
            raise DataPlaneContractError("overlay-only changes cannot alter feature rows")

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
        if self.schema_version != FEATURE_ENVELOPE_VERSION:
            raise DataPlaneContractError("feature envelope schema version is invalid")
        for name in (
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
            _require_text(getattr(self, name), name)
        if self.merge_epoch != self.market_change_set.merge_epoch:
            raise DataPlaneContractError("feature envelope and changes must share merge_epoch")
        codes = tuple(feature.quote.code for feature in self.feature_snapshots)
        if codes != tuple(sorted(codes)) or len(codes) != len(set(codes)):
            raise DataPlaneContractError("feature snapshots must be sorted and unique")


def _require_text(value: str, name: str) -> None:
    if not value.strip():
        raise DataPlaneContractError(f"{name} must not be empty")


__all__ = [
    "FEATURE_ENVELOPE_VERSION",
    "LOGICAL_CACHE_LIMIT_BYTES",
    "MARKET_CHANGE_SET_VERSION",
    "PROCESS_PEAK_RSS_LIMIT_BYTES",
    "DataPlaneContractError",
    "FeatureSnapshotEnvelope",
    "MarketChangeSet",
]

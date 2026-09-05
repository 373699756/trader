"""Pure audit contract for the BaoStock V3 holdout boundary."""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Literal

from trader.domain.research.h1_point_in_time import canonical_hash

BAOSTOCK_HOLDOUT_ISOLATION_CONTRACT = "baostock_holdout_isolation_contract"
BAOSTOCK_DAILY_IDENTITY = "score_baostock_daily_core_v2"
BAOSTOCK_SOURCE_ANCHOR = "15:00_daily_close"
LEGACY_HOLDOUT_IDENTITY = "score_tomorrow_historical_candidate"
POINT_IN_TIME_HOLDOUT_IDENTITY = "tomorrow_v3_point_in_time_holdout"

_POINT_IN_TIME_RESERVE_DAYS = 200
_SHA256 = re.compile(r"[0-9a-f]{64}")

BaoStockHoldoutIsolationStatus = Literal["isolated", "blocked"]


class BaoStockHoldoutIsolationBlocker(str, Enum):
    """Closed set of research eligibility failures reported by the audit."""

    DAILY_IDENTITY_MISMATCH = "daily_identity_mismatch"
    POINT_IN_TIME_RESERVE_BELOW_200 = "point_in_time_reserve_below_200"
    POINT_IN_TIME_RESERVE_NOT_LATEST = "point_in_time_reserve_not_latest"
    POINT_IN_TIME_RESERVE_CONSUMED = "point_in_time_reserve_consumed"
    DAILY_SOURCE_ANCHOR_INVALID = "daily_source_anchor_invalid"
    DAILY_SOURCE_CLAIMS_POINT_IN_TIME = "daily_source_claims_point_in_time"
    POINT_IN_TIME_HOLDOUT_ALREADY_OPENED = "point_in_time_holdout_already_opened"
    LEGACY_HOLDOUT_IDENTITY_MISMATCH = "legacy_holdout_identity_mismatch"
    NEW_HOLDOUT_IDENTITY_MISMATCH = "new_holdout_identity_mismatch"
    LEGACY_HOLDOUT_REUSED_AS_PARENT = "legacy_holdout_reused_as_parent"
    REQUIRED_PARENT_HASH_MISSING = "required_parent_hash_missing"


@dataclass(frozen=True)
class BaoStockHoldoutIsolationInput:
    """Frozen manifest metadata consumed without reading any market rows."""

    daily_identity: str
    daily_manifest_hash: str
    split_manifest_hash: str
    ordered_complete_dates: tuple[date, ...]
    training_consumed_dates: tuple[date, ...]
    confirmation_consumed_dates: tuple[date, ...]
    daily_proxy_consumed_dates: tuple[date, ...]
    point_in_time_reserved_dates: tuple[date, ...]
    source_anchor: str
    point_in_time_parity_claimed: bool
    point_in_time_holdout_opened: bool
    legacy_holdout_identity: str
    legacy_holdout_hash: str
    new_holdout_identity: str
    new_holdout_parent_hashes: tuple[str, ...]
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        hashes = (
            self.daily_manifest_hash,
            self.split_manifest_hash,
            self.legacy_holdout_hash,
            *self.new_holdout_parent_hashes,
        )
        if any(_SHA256.fullmatch(value) is None for value in hashes):
            raise ValueError("BaoStock holdout metadata requires lowercase SHA-256 hashes")
        if self.daily_manifest_hash == self.split_manifest_hash:
            raise ValueError("BaoStock daily and split manifest hashes must be distinct")
        if len(set(self.new_holdout_parent_hashes)) != len(self.new_holdout_parent_hashes):
            raise ValueError("BaoStock holdout parent hashes must be unique")

        _require_strictly_increasing(self.ordered_complete_dates, required=True)
        consumed_groups = (
            self.training_consumed_dates,
            self.confirmation_consumed_dates,
            self.daily_proxy_consumed_dates,
            self.point_in_time_reserved_dates,
        )
        for values in consumed_groups:
            _require_strictly_increasing(values, required=False)
        complete_dates = set(self.ordered_complete_dates)
        if any(not set(values) <= complete_dates for values in consumed_groups):
            raise ValueError("BaoStock consumed and reserved dates must belong to the complete calendar")

        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class BaoStockHoldoutIsolationAudit:
    contract_identity: str
    input_content_hash: str
    daily_manifest_hash: str
    split_manifest_hash: str
    legacy_holdout_identity: str
    new_holdout_identity: str
    source_anchor: str
    reserved_date_count: int
    status: BaoStockHoldoutIsolationStatus
    blockers: tuple[BaoStockHoldoutIsolationBlocker, ...]
    point_in_time_parity: bool
    terminal_holdout_opened: bool
    production_authority: bool
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        if self.contract_identity != BAOSTOCK_HOLDOUT_ISOLATION_CONTRACT:
            raise ValueError("BaoStock holdout isolation contract identity is invalid")
        if _SHA256.fullmatch(self.input_content_hash) is None or any(
            _SHA256.fullmatch(value) is None for value in (self.daily_manifest_hash, self.split_manifest_hash)
        ):
            raise ValueError("BaoStock holdout audit requires lowercase SHA-256 hashes")
        if self.reserved_date_count < 0:
            raise ValueError("BaoStock holdout audit reserve count cannot be negative")
        if self.status not in ("isolated", "blocked"):
            raise ValueError("BaoStock holdout audit status is invalid")
        if len(set(self.blockers)) != len(self.blockers):
            raise ValueError("BaoStock holdout audit blockers must be unique")
        if self.status == "isolated" and self.blockers:
            raise ValueError("isolated BaoStock holdout audit cannot contain blockers")
        if self.status == "blocked" and not self.blockers:
            raise ValueError("blocked BaoStock holdout audit requires blockers")
        if self.point_in_time_parity or self.terminal_holdout_opened or self.production_authority:
            raise ValueError("BaoStock daily-close audit cannot grant holdout or production authority")
        object.__setattr__(self, "content_hash", canonical_hash(self))


def audit_baostock_holdout_isolation(
    value: BaoStockHoldoutIsolationInput,
) -> BaoStockHoldoutIsolationAudit:
    """Audit frozen split metadata without defining a split or opening a holdout."""

    blockers = _daily_holdout_blockers(value)
    blockers.extend(_holdout_identity_blockers(value))

    return BaoStockHoldoutIsolationAudit(
        contract_identity=BAOSTOCK_HOLDOUT_ISOLATION_CONTRACT,
        input_content_hash=value.content_hash,
        daily_manifest_hash=value.daily_manifest_hash,
        split_manifest_hash=value.split_manifest_hash,
        legacy_holdout_identity=value.legacy_holdout_identity,
        new_holdout_identity=value.new_holdout_identity,
        source_anchor=value.source_anchor,
        reserved_date_count=len(value.point_in_time_reserved_dates),
        status="blocked" if blockers else "isolated",
        blockers=tuple(blockers),
        point_in_time_parity=False,
        terminal_holdout_opened=False,
        production_authority=False,
    )


def _daily_holdout_blockers(
    value: BaoStockHoldoutIsolationInput,
) -> list[BaoStockHoldoutIsolationBlocker]:
    blockers: list[BaoStockHoldoutIsolationBlocker] = []
    if value.daily_identity != BAOSTOCK_DAILY_IDENTITY:
        blockers.append(BaoStockHoldoutIsolationBlocker.DAILY_IDENTITY_MISMATCH)
    if len(value.point_in_time_reserved_dates) < _POINT_IN_TIME_RESERVE_DAYS:
        blockers.append(BaoStockHoldoutIsolationBlocker.POINT_IN_TIME_RESERVE_BELOW_200)
    if value.point_in_time_reserved_dates != value.ordered_complete_dates[-_POINT_IN_TIME_RESERVE_DAYS:]:
        blockers.append(BaoStockHoldoutIsolationBlocker.POINT_IN_TIME_RESERVE_NOT_LATEST)

    reserved_dates = set(value.point_in_time_reserved_dates)
    consumed_dates = set(value.training_consumed_dates)
    consumed_dates.update(value.confirmation_consumed_dates)
    consumed_dates.update(value.daily_proxy_consumed_dates)
    if reserved_dates & consumed_dates:
        blockers.append(BaoStockHoldoutIsolationBlocker.POINT_IN_TIME_RESERVE_CONSUMED)
    if value.source_anchor != BAOSTOCK_SOURCE_ANCHOR:
        blockers.append(BaoStockHoldoutIsolationBlocker.DAILY_SOURCE_ANCHOR_INVALID)
    if value.point_in_time_parity_claimed:
        blockers.append(BaoStockHoldoutIsolationBlocker.DAILY_SOURCE_CLAIMS_POINT_IN_TIME)
    if value.point_in_time_holdout_opened:
        blockers.append(BaoStockHoldoutIsolationBlocker.POINT_IN_TIME_HOLDOUT_ALREADY_OPENED)
    return blockers


def _holdout_identity_blockers(
    value: BaoStockHoldoutIsolationInput,
) -> list[BaoStockHoldoutIsolationBlocker]:
    blockers: list[BaoStockHoldoutIsolationBlocker] = []
    if value.legacy_holdout_identity != LEGACY_HOLDOUT_IDENTITY:
        blockers.append(BaoStockHoldoutIsolationBlocker.LEGACY_HOLDOUT_IDENTITY_MISMATCH)
    if value.new_holdout_identity != POINT_IN_TIME_HOLDOUT_IDENTITY:
        blockers.append(BaoStockHoldoutIsolationBlocker.NEW_HOLDOUT_IDENTITY_MISMATCH)
    if value.legacy_holdout_hash in value.new_holdout_parent_hashes:
        blockers.append(BaoStockHoldoutIsolationBlocker.LEGACY_HOLDOUT_REUSED_AS_PARENT)
    required_parent_hashes = {value.daily_manifest_hash, value.split_manifest_hash}
    if not required_parent_hashes <= set(value.new_holdout_parent_hashes):
        blockers.append(BaoStockHoldoutIsolationBlocker.REQUIRED_PARENT_HASH_MISSING)
    return blockers


def _require_strictly_increasing(values: tuple[date, ...], *, required: bool) -> None:
    if (required and not values) or any(left >= right for left, right in zip(values, values[1:], strict=False)):
        raise ValueError("BaoStock holdout dates must be strictly increasing")


__all__ = [
    "BAOSTOCK_DAILY_IDENTITY",
    "BAOSTOCK_HOLDOUT_ISOLATION_CONTRACT",
    "BAOSTOCK_SOURCE_ANCHOR",
    "LEGACY_HOLDOUT_IDENTITY",
    "POINT_IN_TIME_HOLDOUT_IDENTITY",
    "BaoStockHoldoutIsolationAudit",
    "BaoStockHoldoutIsolationBlocker",
    "BaoStockHoldoutIsolationInput",
    "BaoStockHoldoutIsolationStatus",
    "audit_baostock_holdout_isolation",
]

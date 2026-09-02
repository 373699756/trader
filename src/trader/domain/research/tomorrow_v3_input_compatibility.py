"""Pure consumer contract for the frozen Tomorrow V3 daily input."""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass
from datetime import date
from typing import Literal

from trader.domain.research.h1_point_in_time import canonical_hash

TomorrowV3InputCompatibilityStatus = Literal["compatible", "incompatible"]

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REASON = re.compile(r"^[a-z0-9_]{1,96}$")
_SOURCE_IDENTITY = "score_baostock_daily_core_v2"
_SOURCE_CUTOFF = date(2026, 8, 31)
_AUTHORITATIVE_SESSIONS = 2000
_PRIMARY_KEY = ("code", "trade_date")
_RAW_QFQ_LAYOUT = "same_row"
_ROW_HASH_ALGORITHM = "sha256"
_INPUT_SCHEMA_VERSION = "tomorrow_v3_frozen_daily_input_v1"

TOMORROW_V3_ALPHA_NAMES = (
    "qfq_return_1d",
    "qfq_return_3d",
    "qfq_return_5d",
    "qfq_residual_momentum_20d_skip5",
    "qfq_residual_momentum_40d_skip5",
    "qfq_residual_momentum_60d_skip5",
)
TOMORROW_V3_ALPHA_UNITS = ("ratio",) * len(TOMORROW_V3_ALPHA_NAMES)


@dataclass(frozen=True, order=True)
class DailyInputField:
    name: str
    unit: str

    def __post_init__(self) -> None:
        if not self.name or not self.name.isidentifier() or not self.unit or len(self.unit) > 64:
            raise ValueError("Tomorrow V3 daily input field contract is invalid")


REQUIRED_DAILY_FIELDS = (
    DailyInputField("code", "security_code"),
    DailyInputField("trade_date", "iso_date"),
    DailyInputField("board", "board_id"),
    DailyInputField("raw_open", "cny_per_share"),
    DailyInputField("raw_high", "cny_per_share"),
    DailyInputField("raw_low", "cny_per_share"),
    DailyInputField("raw_close", "cny_per_share"),
    DailyInputField("raw_pre_close", "cny_per_share"),
    DailyInputField("raw_volume", "shares"),
    DailyInputField("raw_amount", "cny"),
    DailyInputField("raw_pct_change", "ratio"),
    DailyInputField("raw_turnover_rate", "ratio"),
    DailyInputField("trade_status", "supplier_trade_status"),
    DailyInputField("qfq_open", "cny_per_share_qfq"),
    DailyInputField("qfq_high", "cny_per_share_qfq"),
    DailyInputField("qfq_low", "cny_per_share_qfq"),
    DailyInputField("qfq_close", "cny_per_share_qfq"),
    DailyInputField("qfq_volume", "shares"),
    DailyInputField("qfq_amount", "cny"),
)


@dataclass(frozen=True)
class FrozenDailyInputDescriptor:
    """Typed projection exposed by A's future frozen manifest port."""

    manifest_hash: str
    source_identity: str
    source_cutoff: date
    requested_sessions: int
    primary_key: tuple[str, ...]
    fields: tuple[DailyInputField, ...]
    raw_qfq_layout: str
    row_hash_algorithm: str
    frozen: bool
    schema_version: str = _INPUT_SCHEMA_VERSION
    production_authority: bool = False
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        if _SHA256.fullmatch(self.manifest_hash) is None:
            raise ValueError("Tomorrow V3 daily input manifest hash is invalid")
        if not self.fields or len({field.name for field in self.fields}) != len(self.fields):
            raise ValueError("Tomorrow V3 daily input fields must be present and unique")
        object.__setattr__(self, "fields", tuple(sorted(self.fields)))
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class TomorrowV3InputCompatibility:
    status: TomorrowV3InputCompatibilityStatus
    parent_manifest_hash: str
    input_manifest_hash: str
    input_descriptor_hash: str
    feature_names: tuple[str, ...]
    feature_units: tuple[str, ...]
    failure_reasons: tuple[str, ...]
    training_started: bool = False
    terminal_holdout_opened: bool = False
    production_authority: bool = False
    automatic_model_update: bool = False
    schema_version: str = "tomorrow_v3_input_compatibility_v1"
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        hashes = (self.parent_manifest_hash, self.input_manifest_hash, self.input_descriptor_hash)
        if any(_SHA256.fullmatch(value) is None for value in hashes):
            raise ValueError("Tomorrow V3 input compatibility parent identity is invalid")
        reasons = tuple(sorted(set(self.failure_reasons)))
        if any(_REASON.fullmatch(reason) is None for reason in reasons):
            raise ValueError("Tomorrow V3 input compatibility failure reason is invalid")
        expected_status = "compatible" if not reasons else "incompatible"
        if self.status != expected_status:
            raise ValueError("Tomorrow V3 input compatibility status is inconsistent")
        if self.feature_names != TOMORROW_V3_ALPHA_NAMES or self.feature_units != TOMORROW_V3_ALPHA_UNITS:
            raise ValueError("Tomorrow V3 input compatibility alpha contract is invalid")
        if (
            self.training_started
            or self.terminal_holdout_opened
            or self.production_authority
            or self.automatic_model_update
        ):
            raise ValueError("Tomorrow V3 input compatibility cannot train, open holdout, or authorize production")
        if self.schema_version != "tomorrow_v3_input_compatibility_v1":
            raise ValueError("Tomorrow V3 input compatibility schema is invalid")
        object.__setattr__(self, "failure_reasons", reasons)
        object.__setattr__(self, "content_hash", canonical_hash(self))


def evaluate_tomorrow_v3_input_compatibility(
    descriptor: FrozenDailyInputDescriptor,
    *,
    expected_manifest_hash: str,
) -> TomorrowV3InputCompatibility:
    """Validate only B's metadata consumption boundary, without reading source rows."""

    if _SHA256.fullmatch(expected_manifest_hash) is None:
        raise ValueError("Tomorrow V3 expected manifest hash is invalid")
    reasons: list[str] = []
    _append_if(descriptor.source_identity != _SOURCE_IDENTITY, "source_identity_invalid", reasons)
    _append_if(descriptor.source_cutoff != _SOURCE_CUTOFF, "source_cutoff_invalid", reasons)
    _append_if(descriptor.requested_sessions != _AUTHORITATIVE_SESSIONS, "requested_sessions_invalid", reasons)
    _append_if(descriptor.primary_key != _PRIMARY_KEY, "primary_key_invalid", reasons)
    _append_if(descriptor.raw_qfq_layout != _RAW_QFQ_LAYOUT, "raw_qfq_layout_invalid", reasons)
    _append_if(descriptor.row_hash_algorithm != _ROW_HASH_ALGORITHM, "row_hash_algorithm_invalid", reasons)
    _append_if(descriptor.schema_version != _INPUT_SCHEMA_VERSION, "input_schema_version_invalid", reasons)
    _append_if(descriptor.manifest_hash != expected_manifest_hash, "manifest_hash_mismatch", reasons)
    _append_if(not descriptor.frozen, "input_not_frozen", reasons)
    _append_if(descriptor.production_authority, "production_authority_forbidden", reasons)
    supplied_fields = {field.name: field.unit for field in descriptor.fields}
    for required in REQUIRED_DAILY_FIELDS:
        supplied_unit = supplied_fields.get(required.name)
        if supplied_unit is None:
            reasons.append(f"field_{required.name}_missing")
        elif supplied_unit != required.unit:
            reasons.append(f"field_{required.name}_unit_invalid")
    return TomorrowV3InputCompatibility(
        status="compatible" if not reasons else "incompatible",
        parent_manifest_hash=expected_manifest_hash,
        input_manifest_hash=descriptor.manifest_hash,
        input_descriptor_hash=descriptor.content_hash,
        feature_names=TOMORROW_V3_ALPHA_NAMES,
        feature_units=TOMORROW_V3_ALPHA_UNITS,
        failure_reasons=tuple(reasons),
    )


def _append_if(condition: bool, reason: str, reasons: list[str]) -> None:
    if condition:
        reasons.append(reason)


__all__ = [
    "DailyInputField",
    "FrozenDailyInputDescriptor",
    "REQUIRED_DAILY_FIELDS",
    "TOMORROW_V3_ALPHA_NAMES",
    "TOMORROW_V3_ALPHA_UNITS",
    "TomorrowV3InputCompatibility",
    "TomorrowV3InputCompatibilityStatus",
    "evaluate_tomorrow_v3_input_compatibility",
]

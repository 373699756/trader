"""Pure, immutable identity consistency checks for the historical baseline."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Literal

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
BaselineIdentityStatus = Literal[
    "baseline_identity_consistent",
    "baseline_identity_inconsistent",
    "live_identity_unverified",
]
BaselineClaimStatus = Literal["matched", "mismatched", "unavailable"]


@dataclass(frozen=True)
class BaselineIdentityClaim:
    """One comparison bound to the source content that supplied it."""

    name: str
    expected: str
    actual: str | None
    source: str
    source_hash: str
    required: bool = True
    status: BaselineClaimStatus = field(init=False)

    def __post_init__(self) -> None:
        if not all((self.name, self.expected, self.source)):
            raise ValueError("baseline identity claim fields must not be empty")
        if _SHA256.fullmatch(self.source_hash) is None:
            raise ValueError("baseline identity claim source hash must be SHA-256")
        status: BaselineClaimStatus = (
            "unavailable" if self.actual is None else "matched" if self.actual == self.expected else "mismatched"
        )
        object.__setattr__(self, "status", status)


@dataclass(frozen=True)
class BaselineIdentityAudit:
    """Reproducible report; this value never grants production authority."""

    claims: tuple[BaselineIdentityClaim, ...]
    live_identity_available: bool = False
    schema_version: str = "score_current_baseline_consistency_audit"
    production_authority: bool = False
    static_status: Literal["baseline_identity_consistent", "baseline_identity_inconsistent"] = field(init=False)
    status: BaselineIdentityStatus = field(init=False)
    conflicts: tuple[str, ...] = field(init=False)
    unavailable: tuple[str, ...] = field(init=False)
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != "score_current_baseline_consistency_audit" or self.production_authority:
            raise ValueError("baseline identity audit contract is fixed and read-only")
        claims = tuple(sorted(self.claims, key=lambda claim: claim.name))
        if not claims or len({claim.name for claim in claims}) != len(claims):
            raise ValueError("baseline identity claims must be non-empty and unique")
        conflicts = tuple(
            claim.name
            for claim in claims
            if claim.status == "mismatched" or (claim.status == "unavailable" and claim.required)
        )
        unavailable = tuple(claim.name for claim in claims if claim.status == "unavailable")
        static_status: Literal["baseline_identity_consistent", "baseline_identity_inconsistent"] = (
            "baseline_identity_inconsistent" if conflicts else "baseline_identity_consistent"
        )
        status: BaselineIdentityStatus
        if static_status == "baseline_identity_inconsistent":
            status = "baseline_identity_inconsistent"
        elif unavailable or not self.live_identity_available:
            status = "live_identity_unverified"
        else:
            status = "baseline_identity_consistent"
        object.__setattr__(self, "claims", claims)
        object.__setattr__(self, "conflicts", conflicts)
        object.__setattr__(self, "unavailable", unavailable)
        object.__setattr__(self, "static_status", static_status)
        object.__setattr__(self, "status", status)
        payload = {
            "claims": [
                {
                    "name": claim.name,
                    "expected": claim.expected,
                    "actual": claim.actual,
                    "source": claim.source,
                    "source_hash": claim.source_hash,
                    "required": claim.required,
                    "status": claim.status,
                }
                for claim in claims
            ],
            "live_identity_available": self.live_identity_available,
            "schema_version": self.schema_version,
            "production_authority": self.production_authority,
            "static_status": static_status,
            "status": status,
            "conflicts": conflicts,
            "unavailable": unavailable,
        }
        encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
        object.__setattr__(self, "content_hash", hashlib.sha256(encoded).hexdigest())


def source_hash(text: str) -> str:
    """Hash a source descriptor for unavailable/runtime claims."""

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


__all__ = [
    "BaselineClaimStatus",
    "BaselineIdentityAudit",
    "BaselineIdentityClaim",
    "BaselineIdentityStatus",
    "source_hash",
]

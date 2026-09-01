from __future__ import annotations

import pytest

from trader.domain.research.baseline_identity import (
    BaselineIdentityAudit,
    BaselineIdentityClaim,
    source_hash,
)


def _claim(name: str, actual: str | None = "ok", *, required: bool = True) -> BaselineIdentityClaim:
    return BaselineIdentityClaim(name, "ok", actual, "fixture", source_hash(name), required)


def test_baseline_identity_audit_is_deterministic_and_distinguishes_live_unknown() -> None:
    audit = BaselineIdentityAudit((_claim("model"),))
    assert audit.status == "live_identity_unverified"
    assert audit.static_status == "baseline_identity_consistent"
    assert audit.conflicts == ()
    assert audit.unavailable == ()
    assert len(audit.content_hash) == 64
    assert audit.content_hash == BaselineIdentityAudit((_claim("model"),)).content_hash


def test_baseline_identity_mismatch_blocks_h1_independent_of_live_status() -> None:
    audit = BaselineIdentityAudit((_claim("model", "wrong"),), live_identity_available=True)
    assert audit.status == "baseline_identity_inconsistent"
    assert audit.static_status == "baseline_identity_inconsistent"
    assert audit.conflicts == ("model",)


def test_baseline_identity_unavailable_is_explicit_and_not_a_conflict() -> None:
    audit = BaselineIdentityAudit((_claim("runtime", None, required=False),), live_identity_available=False)
    assert audit.status == "live_identity_unverified"
    assert audit.unavailable == ("runtime",)


def test_required_static_identity_unavailable_blocks_h1() -> None:
    audit = BaselineIdentityAudit((_claim("p2-report", None),), live_identity_available=True)
    assert audit.status == "baseline_identity_inconsistent"
    assert audit.static_status == "baseline_identity_inconsistent"
    assert audit.conflicts == ("p2-report",)


def test_baseline_identity_rejects_duplicate_or_bad_source() -> None:
    with pytest.raises(ValueError, match="unique"):
        BaselineIdentityAudit((_claim("model"), _claim("model")))
    with pytest.raises(ValueError, match="SHA-256"):
        BaselineIdentityClaim("model", "ok", "ok", "fixture", "bad")

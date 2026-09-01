from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from trader.domain.market.eligibility import IssuerEligibilityFact, IssuerEligibilityReason
from trader.infra.persistence.issuer_eligibility import (
    IssuerEligibilityConflictError,
    SQLiteIssuerEligibilityRegistry,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
OBSERVED_AT = datetime(2026, 9, 1, 10, 0, tzinfo=SHANGHAI)


def _fact(*, evidence_hash: str = "a" * 64) -> IssuerEligibilityFact:
    return IssuerEligibilityFact(
        code="600001",
        reason=IssuerEligibilityReason.CONFIRMED_FINANCIAL_FRAUD,
        effective_at=OBSERVED_AT,
        evidence_id="announcement:fraud-1",
        source="issuer_disclosure",
        evidence_hash=evidence_hash,
    )


def test_registry_is_idempotent_persistent_and_filters_only_after_effective_time(tmp_path) -> None:
    database = tmp_path / "issuer-eligibility.sqlite3"
    registry = SQLiteIssuerEligibilityRegistry(database)

    assert registry.record((_fact(),)) == 1
    assert registry.record((_fact(),)) == 0
    assert registry.filter_codes(("600001", "600002"), OBSERVED_AT - timedelta(seconds=1)) == (
        "600001",
        "600002",
    )
    assert registry.filter_codes(("600001", "600002"), OBSERVED_AT) == ("600002",)

    recovered = SQLiteIssuerEligibilityRegistry(database)
    assert recovered.exclusions(OBSERVED_AT)[0].code == "600001"
    assert recovered.status().excluded_count == 1
    assert recovered.status().fact_count == 1
    assert len(recovered.status().manifest_hash) == 64


def test_registry_rejects_same_evidence_identity_with_different_content(tmp_path) -> None:
    registry = SQLiteIssuerEligibilityRegistry(tmp_path / "issuer-eligibility.sqlite3")
    registry.record((_fact(),))

    with pytest.raises(IssuerEligibilityConflictError):
        registry.record((_fact(evidence_hash="b" * 64),))


def test_registry_detects_tampering_without_silently_clearing_exclusions(tmp_path) -> None:
    database = tmp_path / "issuer-eligibility.sqlite3"
    registry = SQLiteIssuerEligibilityRegistry(database)
    registry.record((_fact(),))
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE issuer_eligibility_facts SET content_hash = ?", ("0" * 64,))
        connection.commit()

    damaged = SQLiteIssuerEligibilityRegistry(database)

    assert damaged.status().integrity_ok is False
    assert damaged.status().last_error == "eligibility_integrity_error"
    assert damaged.filter_codes(("600001",), OBSERVED_AT) == ()

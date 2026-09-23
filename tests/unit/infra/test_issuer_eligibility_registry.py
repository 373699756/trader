from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from trader.recommendation.domain.market.eligibility import IssuerEligibilityFact, IssuerEligibilityReason
from trader.recommendation.infra.persistence.issuer_eligibility import (
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
    root = tmp_path / "blacklist"
    registry = SQLiteIssuerEligibilityRegistry(root)

    assert registry.record((_fact(),)) == 1
    assert registry.record((_fact(),)) == 0
    assert registry.filter_codes(("600001", "600002"), OBSERVED_AT - timedelta(seconds=1)) == (
        "600001",
        "600002",
    )
    assert registry.filter_codes(("600001", "600002"), OBSERVED_AT) == ("600002",)

    recovered = SQLiteIssuerEligibilityRegistry(root)
    assert recovered.exclusions(OBSERVED_AT)[0].code == "600001"
    assert recovered.status().excluded_count == 1
    assert recovered.status().fact_count == 1
    assert len(recovered.status().manifest_hash) == 64


def test_registry_rejects_same_evidence_identity_with_different_content(tmp_path) -> None:
    registry = SQLiteIssuerEligibilityRegistry(tmp_path / "blacklist")
    registry.record((_fact(),))

    with pytest.raises(IssuerEligibilityConflictError):
        registry.record((_fact(evidence_hash="b" * 64),))


def test_registry_detects_tampering_without_silently_clearing_exclusions(tmp_path) -> None:
    root = tmp_path / "blacklist"
    registry = SQLiteIssuerEligibilityRegistry(root)
    registry.record((_fact(),))
    database = next((root / "snapshots").glob("*/financial_fraud.sqlite3"))
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE issuer_eligibility_facts SET content_hash = ?", ("0" * 64,))
        connection.commit()

    damaged = SQLiteIssuerEligibilityRegistry(root)

    assert damaged.status().integrity_ok is False
    assert damaged.status().last_error == "eligibility_integrity_error"
    assert damaged.filter_codes(("600001",), OBSERVED_AT) == ()


def test_registry_migrates_legacy_single_file_once(tmp_path) -> None:
    legacy = tmp_path / "issuer-eligibility.sqlite3"
    legacy_registry = SQLiteIssuerEligibilityRegistry(tmp_path / "legacy")
    legacy_registry.record((_fact(),))
    legacy_file = next((tmp_path / "legacy" / "snapshots").glob("*/financial_fraud.sqlite3"))
    legacy_file.replace(legacy)

    root = tmp_path / "blacklist"
    assert SQLiteIssuerEligibilityRegistry.migrate_legacy_database(legacy, root) == 1
    assert SQLiteIssuerEligibilityRegistry.migrate_legacy_database(legacy, root) == 0
    migrated = SQLiteIssuerEligibilityRegistry(root, read_only=True)
    assert migrated.filter_codes(("600001",), OBSERVED_AT) == ()


def test_registry_refresh_boundary_is_friday_at_1500(tmp_path) -> None:
    registry = SQLiteIssuerEligibilityRegistry(tmp_path / "blacklist")
    before = datetime(2026, 9, 25, 14, 59, 59, tzinfo=SHANGHAI)
    after = datetime(2026, 9, 25, 15, 0, tzinfo=SHANGHAI)

    assert registry.refresh_due(before) is False
    assert registry.refresh_due(after) is True

    registry.refresh_snapshot(after, source="full_market")
    status = registry.status()
    assert status.refresh_state == "ready"
    assert status.last_refresh_at == after
    assert status.next_refresh_at == datetime(2026, 10, 2, 15, 0, tzinfo=SHANGHAI)
    assert registry.refresh_due(after) is False

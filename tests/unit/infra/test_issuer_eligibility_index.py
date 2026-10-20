from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction
from trader.recommendation.domain.market.eligibility import IssuerEligibilityFact, IssuerEligibilityReason
from trader.recommendation.infra.persistence.issuer_eligibility import (
    IssuerEligibilityConflictError,
    SQLiteIssuerEligibilityIndex,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
OBSERVED_AT = datetime(2026, 9, 1, 10, 0, tzinfo=SHANGHAI)


def _fact(*, evidence_hash: str = "a" * 64) -> IssuerEligibilityFact:
    return IssuerEligibilityFact(
        code="600001",
        reason=IssuerEligibilityReason.HISTORICAL_ST,
        effective_at=OBSERVED_AT,
        evidence_id="security:ever-st",
        source="security_master",
        evidence_hash=evidence_hash,
    )


def test_index_is_idempotent_persistent_and_filters_only_after_effective_time(tmp_path) -> None:
    root = tmp_path / "filter_config"
    index = SQLiteIssuerEligibilityIndex((root) / "eligibility.sqlite3")

    assert index.record((_fact(),)) == 1
    assert index.record((_fact(),)) == 0
    assert index.filter_codes(("600001", "600002"), OBSERVED_AT - timedelta(seconds=1)) == (
        "600001",
        "600002",
    )
    assert index.filter_codes(("600001", "600002"), OBSERVED_AT) == ("600002",)

    recovered = SQLiteIssuerEligibilityIndex((root) / "eligibility.sqlite3")
    assert recovered.exclusions(OBSERVED_AT)[0].code == "600001"
    assert recovered.status().excluded_count == 1
    assert recovered.status().fact_count == 1
    assert len(recovered.status().manifest_hash) == 64


def test_index_rejects_same_evidence_identity_with_different_content(tmp_path) -> None:
    index = SQLiteIssuerEligibilityIndex((tmp_path / "filter_config") / "eligibility.sqlite3")
    index.record((_fact(),))

    with pytest.raises(IssuerEligibilityConflictError):
        index.record((_fact(evidence_hash="b" * 64),))


def test_retired_historical_loss_remains_in_snapshot_without_excluding_issuer(tmp_path) -> None:
    index = SQLiteIssuerEligibilityIndex((tmp_path / "filter_config") / "eligibility.sqlite3")
    legacy = IssuerEligibilityFact(
        code="600001",
        reason=IssuerEligibilityReason.HISTORICAL_AUDITED_LOSS,
        effective_at=OBSERVED_AT,
        evidence_id="financial:600001:2024-12-31",
        source="eastmoney_financial",
        evidence_hash="b" * 64,
    )

    assert index.record((legacy,)) == 1
    assert index.facts() == (legacy,)
    assert index.exclusions(OBSERVED_AT) == ()
    assert index.filter_codes(("600001",), OBSERVED_AT) == ("600001",)
    assert index.status().fact_count == 0
    assert index.status().excluded_count == 0
    assert index.status().reason_counts == ()


def test_index_detects_tampering_without_silently_clearing_exclusions(tmp_path) -> None:
    root = tmp_path / "filter_config"
    index = SQLiteIssuerEligibilityIndex((root) / "eligibility.sqlite3")
    index.record((_fact(),))
    database = root / "eligibility.sqlite3"
    with SqliteSession(database) as connection, SqliteTransaction(connection) as transaction:
        connection.run("UPDATE issuer_eligibility_facts SET content_hash = ?", ("0" * 64,))
        transaction.commit()

    damaged = SQLiteIssuerEligibilityIndex((root) / "eligibility.sqlite3")

    assert damaged.status().integrity_ok is False
    assert damaged.status().last_error == "eligibility_integrity_error"
    assert damaged.filter_codes(("600001",), OBSERVED_AT) == ()


def test_index_migrates_legacy_single_file_once(tmp_path) -> None:
    legacy = tmp_path / "issuer-eligibility.sqlite3"
    legacy_index = SQLiteIssuerEligibilityIndex((tmp_path / "legacy") / "eligibility.sqlite3")
    legacy_index.record((_fact(),))
    legacy_file = tmp_path / "legacy/eligibility.sqlite3"
    legacy_file.replace(legacy)

    root = tmp_path / "filter_config"
    from scripts.runtime_migration import ImportEligibility

    ImportEligibility(tmp_path / "no-manifest", legacy, root / "eligibility.sqlite3")
    ImportEligibility(tmp_path / "no-manifest", legacy, root / "eligibility.sqlite3")
    migrated = SQLiteIssuerEligibilityIndex((root) / "eligibility.sqlite3", read_only=True)
    assert migrated.filter_codes(("600001",), OBSERVED_AT) == ()


def test_index_refresh_boundary_is_friday_at_1500(tmp_path) -> None:
    index = SQLiteIssuerEligibilityIndex((tmp_path / "filter_config") / "eligibility.sqlite3")
    before = datetime(2026, 9, 25, 14, 59, 59, tzinfo=SHANGHAI)
    after = datetime(2026, 9, 25, 15, 0, tzinfo=SHANGHAI)

    assert index.refresh_due(before) is False
    assert index.refresh_due(after) is True

    index.refresh_snapshot(after, source="full_market")
    status = index.status()
    assert status.refresh_state == "ready"
    assert status.last_refresh_at == after
    assert status.next_refresh_at == datetime(2026, 10, 2, 15, 0, tzinfo=SHANGHAI)
    assert index.refresh_due(after) is False

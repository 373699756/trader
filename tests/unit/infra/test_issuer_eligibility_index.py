"""Reference-owned ST stays in memory; non-ST exclusions enter company facts."""

from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from trader.download.application.company_exclusions import PublishCompanyExclusions
from trader.download.infra.company_facts_index import SQLiteCompanyFactsIndex
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction
from trader.recommendation.domain.market.eligibility import IssuerEligibilityFact, IssuerEligibilityReason
from trader.recommendation.infra.market_data.company_eligibility import CompanyEligibility

AT = datetime(2026, 9, 1, 10, tzinfo=ZoneInfo("Asia/Shanghai"))


def _fact():
    return IssuerEligibilityFact(
        "600001", IssuerEligibilityReason.HISTORICAL_DELISTING_WARNING, AT, "quote:delisting", "market_quote", "a" * 64
    )


def _reader(path):
    return CompanyEligibility(PublishCompanyExclusions(SQLiteCompanyFactsIndex(path)))


def test_non_st_exclusion_is_idempotent_and_keeps_effective_time(tmp_path):
    path = tmp_path / "company_facts.sqlite3"
    index = _reader(path)
    assert index.record((_fact(),)) == 1
    assert index.record((_fact(),)) == 0
    assert index.filter_codes(("600001", "600002"), AT - timedelta(seconds=1)) == ("600001", "600002")
    assert index.filter_codes(("600001", "600002"), AT) == ("600002",)
    assert _reader(path).facts() == (_fact(),)
    with SqliteSession(path) as connection:
        assert tuple(
            connection.run("SELECT reason,source,length(evidence_hash) FROM company_exclusion").require_one()
        ) == (2, 2, 32)


def test_st_observation_is_immediate_but_creates_no_company_database(tmp_path):
    path = tmp_path / "company_facts.sqlite3"
    index = _reader(path)
    fact = replace(_fact(), reason=IssuerEligibilityReason.HISTORICAL_ST)
    index.record((fact,))
    assert index.filter_codes(("600001",), AT) == ()
    assert not path.exists()
    assert _reader(path).facts() == ()


def test_conflicting_evidence_cannot_replace_existing_fact(tmp_path):
    index = _reader(tmp_path / "company_facts.sqlite3")
    index.record((_fact(),))
    with pytest.raises(ValueError, match="conflict"):
        index.record((replace(_fact(), evidence_hash="b" * 64),))
    assert index.facts() == (_fact(),)


def test_retired_company_risk_is_not_reintroduced_as_permanent_exclusion(tmp_path):
    index = _reader(tmp_path / "company_facts.sqlite3")
    with pytest.raises(ValueError, match="reason_inactive"):
        index.record((replace(_fact(), reason=IssuerEligibilityReason.HISTORICAL_AUDITED_LOSS),))
    assert index.facts() == ()


def test_corrupt_exclusion_is_rejected_while_loaded_snapshot_stays_available(tmp_path):
    path = tmp_path / "company_facts.sqlite3"
    index = _reader(path)
    index.record((_fact(),))
    with SqliteSession(path) as connection, SqliteTransaction(connection):
        connection.run("PRAGMA ignore_check_constraints=ON")
        connection.run("UPDATE company_exclusion SET reason=99")
    damaged = _reader(path)
    assert not damaged.status().integrity_ok
    assert damaged.filter_codes(("600001",), AT) == ()
    assert index.exclusions(AT)[0].code == "600001"


def test_failed_persistence_keeps_immediate_restriction(tmp_path, monkeypatch):
    index = _reader(tmp_path / "company_facts.sqlite3")

    def fail(facts):
        raise OSError("fixture")

    monkeypatch.setattr(index._publication, "execute", fail)
    with pytest.raises(RuntimeError, match="persistence_failed"):
        index.record((_fact(),))
    assert index.filter_codes(("600001",), AT) == ()
    assert index.status().persistence_error_count == 1

"""Incompatible download stores fail before inventory work and remain unchanged."""

from scripts.core.contracts import DiagnosticResult
from scripts.core.projections import BuildReport
from scripts.download.company.company_query_audit import AuditCompanyDatabase
from tests.helpers.download.company import company_evidence as fixtures
from tests.helpers.shared.configuration import SqlitePolicyFixture
from trader.infra.sqlite_connection import SqliteSession


def test_incompatible_stores_are_projected_without_scanning_or_modifying_them(tmp_path, monkeypatch):
    from scripts.download.company import company_query_audit

    collection, formal = tmp_path / "collection.sqlite3", tmp_path / "facts.sqlite3"
    for path in (collection, formal):
        with SqliteSession(path) as connection:
            connection.run("CREATE TABLE obsolete(value TEXT)")
    before = collection.read_bytes(), formal.read_bytes()

    def forbidden(*args, **kwargs):
        raise AssertionError("incompatible stores must be rejected before inventory scans")

    monkeypatch.setattr(company_query_audit, "LoadCompanySyncConfiguration", forbidden)
    report = AuditCompanyDatabase(collection, facts_path=formal, now=fixtures.NOW,
        budget_seconds=30, sqlite_policy=SqlitePolicyFixture())
    assert report["status"] == "failed"
    database = report["summary"]["database"]
    assert database["incompatible_stores"] == ["collection", "facts"]
    assert not database["complete"] and not database["budget_exhausted"]
    public = BuildReport("company-resume", (DiagnosticResult("company_resume", 1, 0, report, None),))
    assert public["checks"][0]["summary"]["database"]["incompatible_stores"] == ["collection", "facts"]
    assert (collection.read_bytes(), formal.read_bytes()) == before

"""Company restart, durable failure and published coverage regression scenarios."""


import pytest

from tests.helpers.download.company import company_evidence as fixtures
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction
from tests.helpers.download.company_resources import SQLiteCompanyEvidenceReaderFixture as SQLiteCompanyEvidenceReader

NOW = fixtures.NOW
































@pytest.mark.parametrize("number", (3, 4))
def test_company_reader_and_writer_reject_old_columns_without_migration(tmp_path, number):
    import sqlite3
    from tests.helpers.download.configuration import CompanyEventPolicyFixture
    from tests.helpers.shared.configuration import SqlitePolicyFixture
    from trader.download.infra.company_facts_protocol import CompanyExtractionHash, EncodeCompanyTime
    from trader.download.infra.company_facts_schema import COMPANY_FACTS_SCHEMA
    from trader.download.infra.company_facts_index import SQLiteCompanyFactsIndex
    from trader.infra.sqlite_connection import EnsureSqliteSchema, SqliteSchema
    from trader.infra.sqlite_publication import PublishSqliteFile

    path = tmp_path / "company_facts.sqlite3"
    schema = SqliteSchema(tuple(
        statement.replace("CREATE TABLE company_publication (",
                          f"CREATE TABLE company_publication (schema_version INTEGER NOT NULL CHECK(schema_version={number}),")
        for statement in COMPANY_FACTS_SCHEMA.statements
    ))
    with SqliteSession(path) as connection:
        EnsureSqliteSchema(connection, schema, allow_create=True)
        connection.run(f"PRAGMA user_version={number}")
        connection.run(
            "INSERT INTO company_publication(schema_version,singleton,extraction_hash,content_hash,published_at) "
            "VALUES(?,1,?,?,?)",
            (number, CompanyExtractionHash(CompanyEventPolicyFixture()), b"s" * 32, EncodeCompanyTime(NOW)),
        )
    PublishSqliteFile(path, sqlite_policy=SqlitePolicyFixture())
    reader = SQLiteCompanyEvidenceReader(path)
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="company_snapshot_unreadable"):
        reader.publication_version()
    assert path.read_bytes() == before
    writer = SQLiteCompanyFactsIndex(path, event_policy=CompanyEventPolicyFixture(), sqlite_policy=SqlitePolicyFixture())
    with pytest.raises(sqlite3.DatabaseError, match="schema_unsupported"):
        writer.initialize()
    assert path.read_bytes() == before
    with SqliteSession(path) as connection:
        assert connection.run("SELECT schema_version FROM company_publication").require_one()[0] == number
        assert connection.run("PRAGMA user_version").require_one()[0] == number




@pytest.mark.parametrize("leftover", ("empty_file", "rolled_back_schema"))
def test_company_first_initialization_recovers_only_empty_leftover(tmp_path, leftover):
    from tests.helpers.download.configuration import CompanyEventPolicyFixture
    from tests.helpers.shared.configuration import SqlitePolicyFixture
    from trader.download.infra.company_facts_index import SQLiteCompanyFactsIndex

    path = tmp_path / "facts.sqlite3"
    if leftover == "empty_file":
        path.touch()
    else:
        with SqliteSession(path) as connection:
            with pytest.raises(RuntimeError), SqliteTransaction(connection):
                connection.run("CREATE TABLE interrupted_fixture(value INTEGER)")
                raise RuntimeError("first schema creation interrupted")
    writer = SQLiteCompanyFactsIndex(path, event_policy=CompanyEventPolicyFixture(), sqlite_policy=SqlitePolicyFixture())

    writer.initialize()

    with SqliteSession(path) as connection:
        assert connection.run("PRAGMA user_version").require_one()[0] == 0
        assert connection.run("SELECT COUNT(*) FROM company_facts").require_one()[0] == 0

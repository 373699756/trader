"""Each current resource contract rejects incompatible files without mutating another owner."""

import pytest
import sqlite3
from trader.download.infra.company_material_index import SQLiteCompanyMaterialIndex
from tests.helpers.shared.configuration import SqlitePolicyFixture

from tests.helpers.download.company import company_evidence as fixtures
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction


@pytest.mark.parametrize(("database_role", "statement"), (
    ("collection", "DROP TABLE document_retries"),
    ("collection", "DROP TABLE document_extractions"),
    ("collection", "DROP TABLE document_announcement_numbers"),
    ("collection", "DROP TABLE disclosure_query_policy"),
    ("collection", "DROP TABLE company_document_requirements"),
    ("collection", "DROP TABLE company_material_dependencies"),
    ("collection", "DROP TABLE company_field_repairs"),
    ("collection", "DROP TABLE document_contract_link_rules"),
    ("collection", "DROP TABLE document_tasks"),
    ("collection", "DROP TABLE company_annual_income_observation"),
    ("collection", "DROP TABLE company_round_page"),
    ("collection", "DROP TABLE download_failure"),
    ("collection", "DROP INDEX disclosure_query_source_rows"),
    ("collection", "ALTER TABLE document_retries RENAME COLUMN ocr_policy_id TO removed_policy"),
    ("collection", "CREATE TABLE contract(schema_id TEXT)"),
    ("formal", "DROP TABLE company_input_receipt"),
    ("formal", "ALTER TABLE company_facts RENAME COLUMN code TO removed_code"),
))
def test_incompatible_pair_is_rejected_before_initialization_or_file_recovery(tmp_path, monkeypatch, database_role, statement):
    index = fixtures._index(tmp_path)
    path = index.path if database_role == "collection" else index.facts_path
    with SqliteSession(path) as connection, SqliteTransaction(connection):
        connection.run(statement)
    before = (index.path.read_bytes(), index.facts_path.read_bytes())

    def forbidden(*_args, **_kwargs):
        pytest.fail("incompatible pair must be rejected before any writer or material recovery")

    material = SQLiteCompanyMaterialIndex(index.path,facts=index._facts,documents=index._documents,
        event_policy=fixtures.CompanyEventPolicy(),sqlite_policy=SqlitePolicyFixture())
    monkeypatch.setattr(material, "validate_material", forbidden)
    if database_role == "collection":
        with pytest.raises((RuntimeError,ValueError), match="persistence_unavailable|contract_mismatch"):
            material.initialize()
    else:
        with pytest.raises(sqlite3.DatabaseError, match="schema_unsupported"):
            index._facts.initialize()
    assert (index.path.read_bytes(), index.facts_path.read_bytes()) == before


def test_invalid_collection_does_not_initialize_an_empty_formal_file(tmp_path):
    index = fixtures._index(tmp_path)
    with SqliteSession(index.path) as connection:
        connection.run("DROP TABLE document_tasks")
    # Represent an uninitialized file, rather than an older structure to upgrade.
    index.facts_path.write_bytes(b"")
    before = index.path.read_bytes()
    material = SQLiteCompanyMaterialIndex(index.path,facts=index._facts,documents=index._documents,
        event_policy=fixtures.CompanyEventPolicy(),sqlite_policy=SqlitePolicyFixture())
    with pytest.raises((RuntimeError,ValueError), match="persistence_unavailable|contract_mismatch"):
        material.initialize()
    assert index.path.read_bytes() == before and index.facts_path.read_bytes() == b""

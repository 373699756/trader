"""Current company structures are admitted together before either writer runs."""

import pytest

from tests.helpers.download.company import company_evidence as fixtures
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction


@pytest.mark.parametrize(("store", "statement"), (
    ("collection", "DROP TABLE document_retries"),
    ("collection", "DROP TABLE document_extractions"),
    ("collection", "DROP TABLE document_announcement_numbers"),
    ("collection", "DROP TABLE disclosure_query_policy"),
    ("collection", "DROP TABLE company_document_requirements"),
    ("collection", "DROP TABLE company_material_dependencies"),
    ("collection", "DROP TABLE company_field_repairs"),
    ("collection", "DROP TABLE document_contract_link_rules"),
    ("collection", "DROP TABLE annual_income"),
    ("collection", "DROP TABLE download_failure"),
    ("collection", "DROP INDEX disclosure_query_source_rows"),
    ("collection", "ALTER TABLE document_retries RENAME COLUMN ocr_policy_id TO removed_policy"),
    ("collection", "DELETE FROM contract"),
    ("formal", "DROP TABLE company_input_receipt"),
    ("formal", "ALTER TABLE company_facts RENAME COLUMN code TO removed_code"),
))
def test_incompatible_pair_is_rejected_before_initialization_or_file_recovery(tmp_path, monkeypatch, store, statement):
    index = fixtures._index(tmp_path)
    path = index.path if store == "collection" else index.facts_path
    with SqliteSession(path) as connection, SqliteTransaction(connection):
        connection.run(statement)
    before = (index.path.read_bytes(), index.facts_path.read_bytes())

    def forbidden(*_args, **_kwargs):
        pytest.fail("incompatible pair must be rejected before any writer or material recovery")

    monkeypatch.setattr(index._facts, "initialize", forbidden)
    monkeypatch.setattr(index, "_repair_pending_files", forbidden)
    with pytest.raises(ValueError, match="database_contract_mismatch"):
        index.initialize()
    assert (index.path.read_bytes(), index.facts_path.read_bytes()) == before


def test_invalid_collection_does_not_initialize_an_empty_formal_file(tmp_path):
    index = fixtures._index(tmp_path)
    with SqliteSession(index.path) as connection:
        connection.run("DROP TABLE annual_income")
    # Represent an uninitialized file, rather than an older structure to upgrade.
    index.facts_path.write_bytes(b"")
    before = index.path.read_bytes()
    with pytest.raises(ValueError, match="database_contract_mismatch"):
        index.initialize()
    assert index.path.read_bytes() == before and index.facts_path.read_bytes() == b""

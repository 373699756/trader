import sqlite3
import tempfile

from stock_analyzer.sqlite_support import sqlite_transaction
from stock_analyzer.validation_schema import ValidationSchemaManager


def _index_names(db_path: str, table_name: str):
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(f"PRAGMA index_list({table_name})").fetchall()
    return {row[1] for row in rows}


def _migration_ids(db_path: str):
    with sqlite3.connect(db_path) as conn:
        return [row[0] for row in conn.execute("SELECT migration_id FROM schema_migrations ORDER BY migration_id").fetchall()]


def test_validation_schema_migrations_are_idempotent_and_create_query_indexes():
    with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as handle:
        db_path = handle.name

    manager = ValidationSchemaManager(sqlite_transaction, db_path)
    manager.init_db()

    signal_indexes = _index_names(db_path, "strategy_signals")
    outcome_indexes = _index_names(db_path, "strategy_outcomes")
    migration_ids = _migration_ids(db_path)

    assert "0011_add_query_indexes" in migration_ids
    assert "idx_strategy_signals_code_date" in signal_indexes
    assert "idx_strategy_outcomes_code_trade_date" in outcome_indexes

    manager.init_db()
    assert migration_ids == _migration_ids(db_path)

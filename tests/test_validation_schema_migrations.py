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


def test_snapshot_phase_unique_key_migration_preserves_signal_outcome_foreign_keys():
    with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as handle:
        db_path = handle.name

    manager = ValidationSchemaManager(sqlite_transaction, db_path)
    manager.init_db()
    with sqlite_transaction(db_path) as conn:
        conn.execute(
            """
            INSERT INTO strategy_signals
            (id, strategy_name, strategy_version, signal_date, signal_time, rank, code,
             name, market, reasons_json, raw_json, snapshot_phase, created_at)
            VALUES (1, 'tomorrow_picks', 'v1', '2026-07-08', '2026-07-08T14:30:00',
                    1, '600001', '样本', 'main', '[]', '{}', 'preclose_tradeable', 'now')
            """
        )
        conn.execute(
            """
            INSERT INTO strategy_outcomes (signal_id, code, next_trade_date, updated_at)
            VALUES (1, '600001', '2026-07-09', 'now')
            """
        )

    with sqlite_transaction(db_path) as conn:
        manager._run_migration(
            conn,
            "0018_snapshot_phase_unique_keys",
            ValidationSchemaManager._migration_snapshot_phase_unique_keys,
        )
        signal_ids = conn.execute("SELECT id FROM strategy_signals").fetchall()
        outcome_ids = conn.execute("SELECT signal_id FROM strategy_outcomes").fetchall()
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()

    assert signal_ids == [(1,)]
    assert outcome_ids == [(1,)]
    assert violations == []

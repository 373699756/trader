from __future__ import annotations


class ValidationSchemaManager:
    """Owns SQLite schema creation and lightweight migrations."""

    def __init__(self, connect_fn, db_path: str) -> None:
        self._connect = connect_fn
        self.db_path = db_path

    def init_db(self) -> None:
        with self._connect(self.db_path) as conn:
            self._create_schema_migrations_table(conn)
            for migration_id, migration in self._migrations():
                if self._migration_already_applied(conn, migration_id):
                    continue
                self._run_migration(conn, migration_id, migration)

    @staticmethod
    def _create_schema_migrations_table(conn) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                migration_id TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )

    @staticmethod
    def _migration_already_applied(conn, migration_id: str) -> bool:
        return (
            conn.execute(
                "SELECT 1 FROM schema_migrations WHERE migration_id = ? LIMIT 1",
                (migration_id,),
            ).fetchone()
            is not None
        )

    @staticmethod
    def _record_migration(conn, migration_id: str) -> None:
        conn.execute(
            """
            INSERT OR IGNORE INTO schema_migrations (migration_id, applied_at)
            VALUES (?, CURRENT_TIMESTAMP)
            """,
            (migration_id,),
        )

    def _run_migration(self, conn, migration_id: str, migration) -> None:
        foreign_keys_disabled = migration_id == "0018_snapshot_phase_unique_keys"
        if foreign_keys_disabled:
            conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN IMMEDIATE")
        try:
            migration(conn)
            self._record_migration(conn, migration_id)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            if foreign_keys_disabled:
                conn.execute("PRAGMA foreign_keys = ON")

    @staticmethod
    def _migrations():
        return (
            ("0001_bootstrap_schema", ValidationSchemaManager._migration_bootstrap_schema),
            ("0002_strategy_outcome_columns", ValidationSchemaManager._migration_strategy_outcome_columns),
            ("0003_strategy_signal_batch_columns", ValidationSchemaManager._migration_batch_columns),
            ("0004_candidate_snapshot_columns", ValidationSchemaManager._migration_candidate_columns),
            ("0005_execution_record_columns", ValidationSchemaManager._migration_execution_record_columns),
            ("0006_shadow_outcome_columns", ValidationSchemaManager._migration_shadow_columns),
            ("0007_portfolio_baseline_columns", ValidationSchemaManager._migration_portfolio_baseline_columns),
            ("0008_fold_prediction_columns", ValidationSchemaManager._migration_fold_prediction_columns),
            (
                "0009_seed_signal_batches",
                ValidationSchemaManager._migration_seed_signal_batches_from_signals,
            ),
            ("0010_migration_noop", ValidationSchemaManager._migration_noop),
            (
                "0011_add_query_indexes",
                ValidationSchemaManager._migration_add_query_indexes,
            ),
            ("0012_deepseek_feature_pipeline", ValidationSchemaManager._migration_deepseek_feature_pipeline),
            ("0013_deepseek_daily_call_limit", ValidationSchemaManager._migration_deepseek_daily_call_limit),
            ("0014_sample_type_columns", ValidationSchemaManager._migration_sample_type_columns),
            ("0015_oos_report_experiment_audit", ValidationSchemaManager._migration_oos_report_experiment_audit),
            ("0016_pit_market_snapshots", ValidationSchemaManager._migration_pit_market_snapshots),
            ("0017_snapshot_phase_columns", ValidationSchemaManager._migration_snapshot_phase_columns),
            ("0018_snapshot_phase_unique_keys", ValidationSchemaManager._migration_snapshot_phase_unique_keys),
            ("0019_deepseek_budget_dimensions", ValidationSchemaManager._migration_deepseek_budget_dimensions),
        )

    @staticmethod
    def _migration_bootstrap_schema(conn) -> None:
        for statement in _TABLES:
            conn.execute(statement)
        for statement in _INDEXES:
            conn.execute(statement)

    @staticmethod
    def _migration_strategy_outcome_columns(conn) -> None:
        ValidationSchemaManager._add_columns(conn, "strategy_outcomes", _OUTCOME_MIGRATION_COLUMNS)

    @staticmethod
    def _migration_batch_columns(conn) -> None:
        ValidationSchemaManager._add_columns(conn, "strategy_signal_batches", _BATCH_MIGRATION_COLUMNS)

    @staticmethod
    def _migration_candidate_columns(conn) -> None:
        ValidationSchemaManager._add_columns(conn, "strategy_candidate_snapshots", _CANDIDATE_MIGRATION_COLUMNS)

    @staticmethod
    def _migration_execution_record_columns(conn) -> None:
        ValidationSchemaManager._add_columns(conn, "strategy_execution_records", _EXECUTION_MIGRATION_COLUMNS)

    @staticmethod
    def _migration_shadow_columns(conn) -> None:
        ValidationSchemaManager._add_columns(conn, "strategy_deepseek_shadow_outcomes", _SHADOW_OUTCOME_MIGRATION_COLUMNS)

    @staticmethod
    def _migration_portfolio_baseline_columns(conn) -> None:
        ValidationSchemaManager._add_columns(conn, "daily_portfolio_baselines", _PORTFOLIO_BASELINE_MIGRATION_COLUMNS)

    @staticmethod
    def _migration_fold_prediction_columns(conn) -> None:
        ValidationSchemaManager._add_columns(conn, "strategy_fold_predictions", _FOLD_PREDICTION_MIGRATION_COLUMNS)

    @staticmethod
    def _migration_seed_signal_batches_from_signals(conn) -> None:
        conn.execute(
            """
            INSERT OR IGNORE INTO strategy_signal_batches
            (strategy_name, strategy_version, signal_date, signal_time, saved_count, created_at)
            SELECT strategy_name, strategy_version, signal_date, MAX(signal_time), COUNT(*), MIN(created_at)
            FROM strategy_signals
            GROUP BY strategy_name, strategy_version, signal_date
            """
        )

    @staticmethod
    def _migration_noop(conn) -> None:
        return None

    @staticmethod
    def _migration_add_query_indexes(conn) -> None:
        for statement in _QUERY_INDEXES:
            conn.execute(statement)

    @staticmethod
    def _migration_deepseek_feature_pipeline(conn) -> None:
        for statement in _DEEPSEEK_FEATURE_TABLES + _DEEPSEEK_FEATURE_INDEXES:
            conn.execute(statement)

    @staticmethod
    def _migration_deepseek_daily_call_limit(conn) -> None:
        ValidationSchemaManager._add_columns(
            conn,
            "deepseek_analysis_batches",
            {"api_called": "INTEGER NOT NULL DEFAULT 0"},
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_deepseek_batches_api_day "
            "ON deepseek_analysis_batches(api_called, requested_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_deepseek_batches_trade_date_api_called "
            "ON deepseek_analysis_batches(substr(requested_at, 1, 10), api_called)"
        )

    @staticmethod
    def _migration_deepseek_budget_dimensions(conn) -> None:
        ValidationSchemaManager._add_columns(
            conn,
            "deepseek_analysis_batches",
            {
                "call_phase": "TEXT NOT NULL DEFAULT ''",
                "budget_bucket": "TEXT NOT NULL DEFAULT ''",
            },
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_deepseek_batches_budget_day "
            "ON deepseek_analysis_batches(substr(requested_at, 1, 10), api_called, budget_bucket, call_phase)"
        )

    @staticmethod
    def _migration_sample_type_columns(conn) -> None:
        ValidationSchemaManager._add_columns(
            conn,
            "strategy_signal_batches",
            _BATCH_MIGRATION_COLUMNS,
        )
        ValidationSchemaManager._add_columns(
            conn,
            "strategy_candidate_snapshots",
            _CANDIDATE_MIGRATION_COLUMNS,
        )

    @staticmethod
    def _migration_oos_report_experiment_audit(conn) -> None:
        ValidationSchemaManager._add_columns(
            conn,
            "strategy_oos_reports",
            _OOS_REPORT_MIGRATION_COLUMNS,
        )

    @staticmethod
    def _migration_pit_market_snapshots(conn) -> None:
        from .pit_snapshot import PIT_SNAPSHOT_INDEXES, PIT_SNAPSHOT_TABLES

        for statement in PIT_SNAPSHOT_TABLES + PIT_SNAPSHOT_INDEXES:
            conn.execute(statement)

    @staticmethod
    def _migration_snapshot_phase_columns(conn) -> None:
        for table in (
            "strategy_signal_batches",
            "strategy_candidate_snapshots",
            "strategy_signals",
            "strategy_deepseek_shadow_signals",
        ):
            ValidationSchemaManager._add_columns(
                conn,
                table,
                {"snapshot_phase": "TEXT NOT NULL DEFAULT 'legacy_unknown'"},
            )
        for statement in _SNAPSHOT_PHASE_INDEXES:
            conn.execute(statement)

    @staticmethod
    def _migration_snapshot_phase_unique_keys(conn) -> None:
        for table, statement in _PHASE_AWARE_TABLES.items():
            ValidationSchemaManager._rebuild_table(conn, table, statement)
        for statement in _INDEXES + _QUERY_INDEXES + _SNAPSHOT_PHASE_INDEXES:
            conn.execute(statement)
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError("foreign key check failed during snapshot phase migration")

    @staticmethod
    def _rebuild_table(conn, table: str, create_statement: str) -> None:
        existing_columns = [
            row[1]
            for row in conn.execute("PRAGMA table_info({})".format(table)).fetchall()
        ]
        if not existing_columns:
            return
        temp_table = "{}__phase_migration".format(table)
        conn.execute("DROP TABLE IF EXISTS {}".format(temp_table))
        conn.execute(create_statement.replace("{table_name}", temp_table))
        target_columns = [
            row[1]
            for row in conn.execute("PRAGMA table_info({})".format(temp_table)).fetchall()
        ]
        copy_columns = [column for column in target_columns if column in existing_columns]
        quoted_columns = ", ".join(copy_columns)
        conn.execute(
            "INSERT OR IGNORE INTO {} ({}) SELECT {} FROM {}".format(
                temp_table,
                quoted_columns,
                quoted_columns,
                table,
            )
        )
        conn.execute("DROP TABLE {}".format(table))
        conn.execute("ALTER TABLE {} RENAME TO {}".format(temp_table, table))

    @staticmethod
    def _add_columns(conn, table: str, columns) -> None:
        existing_columns = {row[1] for row in conn.execute("PRAGMA table_info({})".format(table)).fetchall()}
        for column, column_type in columns.items():
            if column not in existing_columns:
                conn.execute("ALTER TABLE {} ADD COLUMN {} {}".format(table, column, column_type))


_DEEPSEEK_FEATURE_TABLES = (
    """CREATE TABLE IF NOT EXISTS deepseek_analysis_batches (
        batch_id TEXT PRIMARY KEY, strategy_name TEXT NOT NULL, snapshot_id TEXT NOT NULL DEFAULT '',
        cutoff_at TEXT NOT NULL, prompt_version TEXT NOT NULL, feature_schema_version TEXT NOT NULL,
        model_name TEXT NOT NULL DEFAULT '', model_tier TEXT NOT NULL DEFAULT 'flash', market_filter TEXT NOT NULL DEFAULT 'all',
        status TEXT NOT NULL DEFAULT 'pending', api_called INTEGER NOT NULL DEFAULT 0,
        call_phase TEXT NOT NULL DEFAULT '', budget_bucket TEXT NOT NULL DEFAULT '',
        request_hash TEXT NOT NULL DEFAULT '', response_hash TEXT NOT NULL DEFAULT '',
        candidate_count INTEGER NOT NULL DEFAULT 0, valid_count INTEGER NOT NULL DEFAULT 0, abstain_count INTEGER NOT NULL DEFAULT 0,
        rejected_count INTEGER NOT NULL DEFAULT 0, prompt_tokens INTEGER NOT NULL DEFAULT 0, completion_tokens INTEGER NOT NULL DEFAULT 0,
        cache_hit_tokens INTEGER NOT NULL DEFAULT 0, cache_miss_tokens INTEGER NOT NULL DEFAULT 0, latency_ms INTEGER NOT NULL DEFAULT 0,
        error_type TEXT NOT NULL DEFAULT '', error_message TEXT NOT NULL DEFAULT '', requested_at TEXT NOT NULL,
        completed_at TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS deepseek_candidate_features (
        id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT NOT NULL, strategy_name TEXT NOT NULL, code TEXT NOT NULL,
        snapshot_id TEXT NOT NULL DEFAULT '', cutoff_at TEXT NOT NULL, completed_at TEXT NOT NULL, expires_at TEXT NOT NULL DEFAULT '',
        prompt_version TEXT NOT NULL, feature_schema_version TEXT NOT NULL, model_name TEXT NOT NULL DEFAULT '', evidence_hash TEXT NOT NULL DEFAULT '',
        evidence_ids_json TEXT NOT NULL DEFAULT '[]', abstain INTEGER NOT NULL DEFAULT 1, valid INTEGER NOT NULL DEFAULT 0,
        validation_error TEXT NOT NULL DEFAULT '', feature_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
        UNIQUE(batch_id, code), FOREIGN KEY(batch_id) REFERENCES deepseek_analysis_batches(batch_id))""",
    """CREATE TABLE IF NOT EXISTS deepseek_counterfactual_outcomes (
        id INTEGER PRIMARY KEY AUTOINCREMENT, strategy_name TEXT NOT NULL, signal_date TEXT NOT NULL,
        strategy_version TEXT NOT NULL DEFAULT '', prompt_version TEXT NOT NULL DEFAULT '', model_name TEXT NOT NULL DEFAULT '',
        local_codes_json TEXT NOT NULL DEFAULT '[]', challenger_codes_json TEXT NOT NULL DEFAULT '[]', replacements_json TEXT NOT NULL DEFAULT '[]',
        local_net_return REAL, challenger_net_return REAL, incremental_net_return REAL, local_max_drawdown REAL, challenger_max_drawdown REAL,
        status TEXT NOT NULL DEFAULT 'pending', outcome_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(strategy_name, signal_date, prompt_version, model_name))""",
)

_DEEPSEEK_FEATURE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_deepseek_batches_strategy_cutoff ON deepseek_analysis_batches(strategy_name, cutoff_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_deepseek_features_strategy_code_cutoff ON deepseek_candidate_features(strategy_name, code, cutoff_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_deepseek_counterfactual_strategy_date ON deepseek_counterfactual_outcomes(strategy_name, signal_date DESC)",
)

_TABLES = (
    *_DEEPSEEK_FEATURE_TABLES,
    """
    CREATE TABLE IF NOT EXISTS strategy_signal_batches (
        strategy_name TEXT NOT NULL,
        strategy_version TEXT NOT NULL,
        signal_date TEXT NOT NULL,
        signal_time TEXT NOT NULL,
        saved_count INTEGER NOT NULL DEFAULT 0,
        candidate_count INTEGER NOT NULL DEFAULT 0,
        selected_count INTEGER NOT NULL DEFAULT 0,
        data_source_timestamp TEXT NOT NULL DEFAULT '',
        market_data_cutoff TEXT NOT NULL DEFAULT '',
        execution_policy_version TEXT NOT NULL DEFAULT '',
        execution_policy_json TEXT NOT NULL DEFAULT '',
        generation_json TEXT NOT NULL DEFAULT '{}',
        portfolio_capital REAL NOT NULL DEFAULT 0,
        snapshot_id TEXT NOT NULL DEFAULT '',
        sample_type TEXT NOT NULL DEFAULT 'unknown',
        sample_source TEXT NOT NULL DEFAULT '',
        snapshot_phase TEXT NOT NULL DEFAULT 'legacy_unknown',
        created_at TEXT NOT NULL,
        PRIMARY KEY(strategy_name, signal_date, strategy_version, snapshot_phase)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS strategy_candidate_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        strategy_name TEXT NOT NULL,
        strategy_version TEXT NOT NULL,
        signal_date TEXT NOT NULL,
        signal_time TEXT NOT NULL,
        code TEXT NOT NULL,
        name TEXT NOT NULL DEFAULT '',
        market TEXT NOT NULL DEFAULT '',
        industry TEXT NOT NULL DEFAULT '',
        style_bucket TEXT NOT NULL DEFAULT 'unknown',
        eligible INTEGER NOT NULL DEFAULT 0,
        selected INTEGER NOT NULL DEFAULT 0,
        rank INTEGER NOT NULL DEFAULT 0,
        score REAL NOT NULL DEFAULT 0,
        point_in_time_valid INTEGER NOT NULL DEFAULT 0,
        eligibility_reasons_json TEXT NOT NULL DEFAULT '[]',
        feature_values_json TEXT NOT NULL DEFAULT '{}',
        missing_mask_json TEXT NOT NULL DEFAULT '{}',
        source_timestamps_json TEXT NOT NULL DEFAULT '{}',
        announcement_time TEXT NOT NULL DEFAULT '',
        market_data_cutoff TEXT NOT NULL DEFAULT '',
        point_in_time_violations_json TEXT NOT NULL DEFAULT '[]',
        sample_type TEXT NOT NULL DEFAULT 'unknown',
        sample_source TEXT NOT NULL DEFAULT '',
        snapshot_phase TEXT NOT NULL DEFAULT 'legacy_unknown',
        raw_json TEXT NOT NULL DEFAULT '{}',
        snapshot_id TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        UNIQUE(strategy_name, strategy_version, signal_date, snapshot_phase, code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS strategy_signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        strategy_name TEXT NOT NULL,
        strategy_version TEXT NOT NULL,
        signal_date TEXT NOT NULL,
        signal_time TEXT NOT NULL,
        rank INTEGER NOT NULL,
        code TEXT NOT NULL,
        name TEXT NOT NULL,
        market TEXT NOT NULL,
        theme TEXT NOT NULL DEFAULT '',
        price_at_signal REAL NOT NULL DEFAULT 0,
        pct_chg_at_signal REAL NOT NULL DEFAULT 0,
        turnover REAL NOT NULL DEFAULT 0,
        volume_ratio REAL NOT NULL DEFAULT 0,
        turnover_rate REAL NOT NULL DEFAULT 0,
        sixty_day_pct REAL NOT NULL DEFAULT 0,
        ytd_pct REAL NOT NULL DEFAULT 0,
        score REAL NOT NULL DEFAULT 0,
        reasons_json TEXT NOT NULL,
        raw_json TEXT NOT NULL,
        snapshot_phase TEXT NOT NULL DEFAULT 'legacy_unknown',
        created_at TEXT NOT NULL,
        UNIQUE(strategy_name, strategy_version, signal_date, snapshot_phase, code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS strategy_outcomes (
        signal_id INTEGER PRIMARY KEY,
        code TEXT NOT NULL,
        next_trade_date TEXT NOT NULL,
        next_open REAL NOT NULL DEFAULT 0,
        next_high REAL NOT NULL DEFAULT 0,
        next_low REAL NOT NULL DEFAULT 0,
        next_close REAL NOT NULL DEFAULT 0,
        next_open_return REAL NOT NULL DEFAULT 0,
        next_close_return REAL NOT NULL DEFAULT 0,
        overnight_return REAL NOT NULL DEFAULT 0,
        intraday_high_return REAL NOT NULL DEFAULT 0,
        future_days INTEGER NOT NULL DEFAULT 1,
        hold_3d_return REAL NOT NULL DEFAULT 0,
        hold_5d_return REAL NOT NULL DEFAULT 0,
        hold_10d_return REAL NOT NULL DEFAULT 0,
        hold_20d_return REAL NOT NULL DEFAULT 0,
        max_gain_3d REAL NOT NULL DEFAULT 0,
        max_drawdown_3d REAL NOT NULL DEFAULT 0,
        hit_3pct INTEGER NOT NULL DEFAULT 0,
        hit_5pct INTEGER NOT NULL DEFAULT 0,
        survivorship_corrected INTEGER NOT NULL DEFAULT 0,
        correction_reason TEXT NOT NULL DEFAULT '',
        trade_cost_pct REAL NOT NULL DEFAULT 0,
        primary_return_field TEXT NOT NULL DEFAULT '',
        primary_return REAL NOT NULL DEFAULT 0,
        primary_return_net REAL NOT NULL DEFAULT 0,
        primary_holding_days INTEGER NOT NULL DEFAULT 0,
        validation_baseline_id TEXT NOT NULL DEFAULT '',
        validation_baseline_json TEXT NOT NULL DEFAULT '',
        label_status TEXT NOT NULL DEFAULT 'settled',
        delisting_status TEXT NOT NULL DEFAULT 'not_applicable',
        execution_policy_version TEXT NOT NULL DEFAULT '',
        execution_policy_json TEXT NOT NULL DEFAULT '',
        cost_scenarios_json TEXT NOT NULL DEFAULT '{}',
        raw_prices_json TEXT NOT NULL DEFAULT '[]',
        benchmark_json TEXT NOT NULL DEFAULT '{}',
        entry_price REAL,
        exit_price REAL,
        return_reproducible INTEGER NOT NULL DEFAULT 0,
        position_status TEXT NOT NULL DEFAULT 'closed',
        entry_trade_date TEXT NOT NULL DEFAULT '',
        earliest_exit_date TEXT NOT NULL DEFAULT '',
        exit_trade_date TEXT NOT NULL DEFAULT '',
        price_adjustment_mode TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL,
        FOREIGN KEY(signal_id) REFERENCES strategy_signals(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS strategy_execution_records (
        signal_id INTEGER PRIMARY KEY,
        code TEXT NOT NULL,
        label_status TEXT NOT NULL DEFAULT 'pending',
        reason TEXT NOT NULL DEFAULT '',
        entry_status TEXT NOT NULL DEFAULT 'pending',
        exit_status TEXT NOT NULL DEFAULT 'pending',
        delisting_status TEXT NOT NULL DEFAULT 'not_applicable',
        promotion_eligible INTEGER NOT NULL DEFAULT 0,
        portfolio_capital REAL NOT NULL DEFAULT 0,
        target_weight_pct REAL NOT NULL DEFAULT 0,
        target_notional REAL NOT NULL DEFAULT 0,
        order_quantity REAL NOT NULL DEFAULT 0,
        actual_filled_quantity REAL NOT NULL DEFAULT 0,
        actual_entry_price REAL,
        actual_exit_quantity REAL NOT NULL DEFAULT 0,
        actual_exit_price REAL,
        unfilled_quantity REAL NOT NULL DEFAULT 0,
        unfilled_entry_quantity REAL NOT NULL DEFAULT 0,
        unfilled_exit_quantity REAL NOT NULL DEFAULT 0,
        fill_source TEXT NOT NULL DEFAULT '',
        fee_pct REAL NOT NULL DEFAULT 0,
        slippage_pct REAL NOT NULL DEFAULT 0,
        impact_pct REAL NOT NULL DEFAULT 0,
        gross_return_pct REAL,
        net_return_pct REAL,
        return_formula TEXT NOT NULL DEFAULT '',
        execution_policy_version TEXT NOT NULL DEFAULT '',
        execution_policy_json TEXT NOT NULL DEFAULT '',
        cost_scenarios_json TEXT NOT NULL DEFAULT '{}',
        raw_prices_json TEXT NOT NULL DEFAULT '[]',
        benchmark_json TEXT NOT NULL DEFAULT '{}',
        position_status TEXT NOT NULL DEFAULT 'not_entered',
        entry_trade_date TEXT NOT NULL DEFAULT '',
        earliest_exit_date TEXT NOT NULL DEFAULT '',
        exit_trade_date TEXT NOT NULL DEFAULT '',
        mark_price REAL,
        price_adjustment_mode TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL,
        FOREIGN KEY(signal_id) REFERENCES strategy_signals(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS strategy_execution_skips (
        signal_id INTEGER PRIMARY KEY,
        code TEXT NOT NULL,
        skip_reason TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL,
        FOREIGN KEY(signal_id) REFERENCES strategy_signals(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS strategy_deepseek_shadow_signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        strategy_name TEXT NOT NULL,
        strategy_version TEXT NOT NULL,
        signal_date TEXT NOT NULL,
        signal_time TEXT NOT NULL,
        rank INTEGER NOT NULL DEFAULT 0,
        local_rank INTEGER NOT NULL DEFAULT 0,
        code TEXT NOT NULL,
        name TEXT NOT NULL DEFAULT '',
        market TEXT NOT NULL DEFAULT '',
        theme TEXT NOT NULL DEFAULT '',
        price_at_signal REAL NOT NULL DEFAULT 0,
        pct_chg_at_signal REAL NOT NULL DEFAULT 0,
        turnover REAL NOT NULL DEFAULT 0,
        volume_ratio REAL NOT NULL DEFAULT 0,
        turnover_rate REAL NOT NULL DEFAULT 0,
        sixty_day_pct REAL NOT NULL DEFAULT 0,
        ytd_pct REAL NOT NULL DEFAULT 0,
        score REAL NOT NULL DEFAULT 0,
        deepseek_rank_score REAL NOT NULL DEFAULT 0,
        deepseek_action TEXT NOT NULL DEFAULT '',
        deepseek_veto INTEGER NOT NULL DEFAULT 0,
        deepseek_penalty REAL NOT NULL DEFAULT 0,
        filter_reason TEXT NOT NULL DEFAULT '',
        raw_json TEXT NOT NULL,
        snapshot_phase TEXT NOT NULL DEFAULT 'legacy_unknown',
        created_at TEXT NOT NULL,
        UNIQUE(strategy_name, strategy_version, signal_date, snapshot_phase, code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS strategy_deepseek_shadow_outcomes (
        shadow_id INTEGER PRIMARY KEY,
        code TEXT NOT NULL,
        next_trade_date TEXT NOT NULL,
        future_days INTEGER NOT NULL DEFAULT 1,
        next_open REAL NOT NULL DEFAULT 0,
        next_close REAL NOT NULL DEFAULT 0,
        next_close_return REAL NOT NULL DEFAULT 0,
        hold_3d_return REAL NOT NULL DEFAULT 0,
        hold_5d_return REAL NOT NULL DEFAULT 0,
        hold_10d_return REAL NOT NULL DEFAULT 0,
        hold_20d_return REAL NOT NULL DEFAULT 0,
        signal_next_close_return REAL NOT NULL DEFAULT 0,
        overnight_return REAL NOT NULL DEFAULT 0,
        signal_hold_3d_return REAL NOT NULL DEFAULT 0,
        signal_hold_5d_return REAL NOT NULL DEFAULT 0,
        signal_hold_10d_return REAL NOT NULL DEFAULT 0,
        signal_hold_20d_return REAL NOT NULL DEFAULT 0,
        exit_return REAL NOT NULL DEFAULT 0,
        signal_exit_return REAL NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(shadow_id) REFERENCES strategy_deepseek_shadow_signals(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS strategy_oos_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        strategy_name TEXT NOT NULL,
        generated_date TEXT NOT NULL,
        generated_at TEXT NOT NULL,
        trigger TEXT NOT NULL DEFAULT 'manual',
        days INTEGER NOT NULL DEFAULT 0,
        oos_status TEXT NOT NULL DEFAULT '',
        baseline_id TEXT NOT NULL DEFAULT '',
        sample_count INTEGER NOT NULL DEFAULT 0,
        real_day_count INTEGER NOT NULL DEFAULT 0,
        avg_primary_return_net REAL NOT NULL DEFAULT 0,
        real_avg_primary_return_net REAL NOT NULL DEFAULT 0,
        real_avg_primary_return_net_ci95_low REAL,
        real_avg_primary_return_net_ci95_high REAL,
        real_portfolio_max_drawdown_pct REAL NOT NULL DEFAULT 0,
        gate_blocked INTEGER NOT NULL DEFAULT 0,
        gate_reason TEXT NOT NULL DEFAULT '',
        experiment_audit_json TEXT NOT NULL DEFAULT '{}',
        report_json TEXT NOT NULL,
        baseline_status_json TEXT NOT NULL,
        validation_gate_json TEXT NOT NULL,
        requirements_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS strategy_fold_predictions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        experiment_id TEXT NOT NULL,
        fold_id TEXT NOT NULL,
        strategy_name TEXT NOT NULL,
        baseline_id TEXT NOT NULL DEFAULT '',
        model_id TEXT NOT NULL DEFAULT '',
        model_version TEXT NOT NULL DEFAULT '',
        train_end_date TEXT NOT NULL DEFAULT '',
        test_date TEXT NOT NULL,
        code TEXT NOT NULL,
        baseline_score REAL,
        predicted_net_return REAL,
        predicted_probability REAL,
        selected INTEGER NOT NULL DEFAULT 0,
        actual_net_return REAL,
        feature_schema_hash TEXT NOT NULL DEFAULT '',
        prediction_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        UNIQUE(experiment_id, fold_id, test_date, code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS daily_portfolio_baselines (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        strategy_name TEXT NOT NULL,
        portfolio_baseline_id TEXT NOT NULL,
        signal_date TEXT NOT NULL,
        signal_time TEXT NOT NULL DEFAULT '',
        strategy_version TEXT NOT NULL DEFAULT '',
        validation_baseline_id TEXT NOT NULL DEFAULT '',
        model_id TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending',
        candidate_hash TEXT NOT NULL DEFAULT '',
        result_json TEXT NOT NULL,
        audit_blob BLOB,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(strategy_name, portfolio_baseline_id, signal_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS strategy_tuning_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        strategy_name TEXT NOT NULL,
        run_time TEXT NOT NULL,
        days INTEGER NOT NULL DEFAULT 20,
        status TEXT NOT NULL DEFAULT '',
        can_apply INTEGER NOT NULL DEFAULT 0,
        shadow_mode INTEGER NOT NULL DEFAULT 1,
        plan_json TEXT NOT NULL,
        metrics_json TEXT NOT NULL,
        deepseek_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stock_prediction_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        prediction_date TEXT NOT NULL,
        prediction_time TEXT NOT NULL,
        code TEXT NOT NULL,
        name TEXT NOT NULL DEFAULT '',
        price_at_signal REAL NOT NULL DEFAULT 0,
        stance TEXT NOT NULL DEFAULT '',
        bias TEXT NOT NULL DEFAULT '',
        timing TEXT NOT NULL DEFAULT '',
        optimization_json TEXT NOT NULL,
        prediction_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(prediction_date, code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stock_prediction_outcomes (
        snapshot_id INTEGER PRIMARY KEY,
        code TEXT NOT NULL,
        next_trade_date TEXT NOT NULL,
        future_days INTEGER NOT NULL DEFAULT 1,
        next_open REAL NOT NULL DEFAULT 0,
        next_close REAL NOT NULL DEFAULT 0,
        next_close_return REAL NOT NULL DEFAULT 0,
        exit_return REAL NOT NULL DEFAULT 0,
        exit_reason TEXT NOT NULL DEFAULT '',
        exit_days INTEGER NOT NULL DEFAULT 0,
        exit_date TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL,
        FOREIGN KEY(snapshot_id) REFERENCES stock_prediction_snapshots(id)
    )
    """,
)

_PHASE_AWARE_TABLES = {
    "strategy_signal_batches": """
    CREATE TABLE IF NOT EXISTS {table_name} (
        strategy_name TEXT NOT NULL,
        strategy_version TEXT NOT NULL,
        signal_date TEXT NOT NULL,
        signal_time TEXT NOT NULL,
        saved_count INTEGER NOT NULL DEFAULT 0,
        candidate_count INTEGER NOT NULL DEFAULT 0,
        selected_count INTEGER NOT NULL DEFAULT 0,
        data_source_timestamp TEXT NOT NULL DEFAULT '',
        market_data_cutoff TEXT NOT NULL DEFAULT '',
        execution_policy_version TEXT NOT NULL DEFAULT '',
        execution_policy_json TEXT NOT NULL DEFAULT '',
        generation_json TEXT NOT NULL DEFAULT '{}',
        portfolio_capital REAL NOT NULL DEFAULT 0,
        snapshot_id TEXT NOT NULL DEFAULT '',
        sample_type TEXT NOT NULL DEFAULT 'unknown',
        sample_source TEXT NOT NULL DEFAULT '',
        snapshot_phase TEXT NOT NULL DEFAULT 'legacy_unknown',
        created_at TEXT NOT NULL,
        PRIMARY KEY(strategy_name, signal_date, strategy_version, snapshot_phase)
    )
    """,
    "strategy_candidate_snapshots": """
    CREATE TABLE IF NOT EXISTS {table_name} (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        strategy_name TEXT NOT NULL,
        strategy_version TEXT NOT NULL,
        signal_date TEXT NOT NULL,
        signal_time TEXT NOT NULL,
        code TEXT NOT NULL,
        name TEXT NOT NULL DEFAULT '',
        market TEXT NOT NULL DEFAULT '',
        industry TEXT NOT NULL DEFAULT '',
        style_bucket TEXT NOT NULL DEFAULT 'unknown',
        eligible INTEGER NOT NULL DEFAULT 0,
        selected INTEGER NOT NULL DEFAULT 0,
        rank INTEGER NOT NULL DEFAULT 0,
        score REAL NOT NULL DEFAULT 0,
        point_in_time_valid INTEGER NOT NULL DEFAULT 0,
        eligibility_reasons_json TEXT NOT NULL DEFAULT '[]',
        feature_values_json TEXT NOT NULL DEFAULT '{}',
        missing_mask_json TEXT NOT NULL DEFAULT '{}',
        source_timestamps_json TEXT NOT NULL DEFAULT '{}',
        announcement_time TEXT NOT NULL DEFAULT '',
        market_data_cutoff TEXT NOT NULL DEFAULT '',
        point_in_time_violations_json TEXT NOT NULL DEFAULT '[]',
        sample_type TEXT NOT NULL DEFAULT 'unknown',
        sample_source TEXT NOT NULL DEFAULT '',
        snapshot_phase TEXT NOT NULL DEFAULT 'legacy_unknown',
        raw_json TEXT NOT NULL DEFAULT '{}',
        snapshot_id TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        UNIQUE(strategy_name, strategy_version, signal_date, snapshot_phase, code)
    )
    """,
    "strategy_signals": """
    CREATE TABLE IF NOT EXISTS {table_name} (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        strategy_name TEXT NOT NULL,
        strategy_version TEXT NOT NULL,
        signal_date TEXT NOT NULL,
        signal_time TEXT NOT NULL,
        rank INTEGER NOT NULL,
        code TEXT NOT NULL,
        name TEXT NOT NULL,
        market TEXT NOT NULL,
        theme TEXT NOT NULL DEFAULT '',
        price_at_signal REAL NOT NULL DEFAULT 0,
        pct_chg_at_signal REAL NOT NULL DEFAULT 0,
        turnover REAL NOT NULL DEFAULT 0,
        volume_ratio REAL NOT NULL DEFAULT 0,
        turnover_rate REAL NOT NULL DEFAULT 0,
        sixty_day_pct REAL NOT NULL DEFAULT 0,
        ytd_pct REAL NOT NULL DEFAULT 0,
        score REAL NOT NULL DEFAULT 0,
        reasons_json TEXT NOT NULL,
        raw_json TEXT NOT NULL,
        snapshot_phase TEXT NOT NULL DEFAULT 'legacy_unknown',
        created_at TEXT NOT NULL,
        UNIQUE(strategy_name, strategy_version, signal_date, snapshot_phase, code)
    )
    """,
    "strategy_deepseek_shadow_signals": """
    CREATE TABLE IF NOT EXISTS {table_name} (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        strategy_name TEXT NOT NULL,
        strategy_version TEXT NOT NULL,
        signal_date TEXT NOT NULL,
        signal_time TEXT NOT NULL,
        rank INTEGER NOT NULL DEFAULT 0,
        local_rank INTEGER NOT NULL DEFAULT 0,
        code TEXT NOT NULL,
        name TEXT NOT NULL DEFAULT '',
        market TEXT NOT NULL DEFAULT '',
        theme TEXT NOT NULL DEFAULT '',
        price_at_signal REAL NOT NULL DEFAULT 0,
        pct_chg_at_signal REAL NOT NULL DEFAULT 0,
        turnover REAL NOT NULL DEFAULT 0,
        volume_ratio REAL NOT NULL DEFAULT 0,
        turnover_rate REAL NOT NULL DEFAULT 0,
        sixty_day_pct REAL NOT NULL DEFAULT 0,
        ytd_pct REAL NOT NULL DEFAULT 0,
        score REAL NOT NULL DEFAULT 0,
        deepseek_rank_score REAL NOT NULL DEFAULT 0,
        deepseek_action TEXT NOT NULL DEFAULT '',
        deepseek_veto INTEGER NOT NULL DEFAULT 0,
        deepseek_penalty REAL NOT NULL DEFAULT 0,
        filter_reason TEXT NOT NULL DEFAULT '',
        raw_json TEXT NOT NULL,
        snapshot_phase TEXT NOT NULL DEFAULT 'legacy_unknown',
        created_at TEXT NOT NULL,
        UNIQUE(strategy_name, strategy_version, signal_date, snapshot_phase, code)
    )
    """,
}

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_strategy_signals_date ON strategy_signals(signal_date)",
    "CREATE INDEX IF NOT EXISTS idx_strategy_signals_strategy_date ON strategy_signals(strategy_name, signal_date DESC)",
    "CREATE INDEX IF NOT EXISTS idx_strategy_signals_strategy_date_rank ON strategy_signals(strategy_name, signal_date DESC, rank)",
    "CREATE INDEX IF NOT EXISTS idx_strategy_signals_strategy_version_date ON strategy_signals(strategy_name, strategy_version, signal_date)",
    "CREATE INDEX IF NOT EXISTS idx_strategy_candidates_strategy_version_date ON strategy_candidate_snapshots(strategy_name, strategy_version, signal_date)",
    "CREATE INDEX IF NOT EXISTS idx_strategy_candidates_selected_eligible ON strategy_candidate_snapshots(strategy_name, strategy_version, signal_date, selected, eligible)",
    "CREATE INDEX IF NOT EXISTS idx_strategy_signal_batches_strategy_date ON strategy_signal_batches(strategy_name, signal_date DESC)",
    "CREATE INDEX IF NOT EXISTS idx_strategy_candidates_strategy_date ON strategy_candidate_snapshots(strategy_name, signal_date DESC)",
    "CREATE INDEX IF NOT EXISTS idx_strategy_candidates_code ON strategy_candidate_snapshots(code)",
    "CREATE INDEX IF NOT EXISTS idx_strategy_outcomes_code ON strategy_outcomes(code)",
    "CREATE INDEX IF NOT EXISTS idx_strategy_outcomes_label_updated ON strategy_outcomes(label_status, updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_strategy_execution_records_status ON strategy_execution_records(label_status)",
    "CREATE INDEX IF NOT EXISTS idx_strategy_execution_skips_code ON strategy_execution_skips(code)",
    "CREATE INDEX IF NOT EXISTS idx_deepseek_shadow_strategy_date ON strategy_deepseek_shadow_signals(strategy_name, signal_date DESC)",
    "CREATE INDEX IF NOT EXISTS idx_deepseek_shadow_code ON strategy_deepseek_shadow_signals(code)",
    "CREATE INDEX IF NOT EXISTS idx_strategy_oos_reports_strategy_time ON strategy_oos_reports(strategy_name, generated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_strategy_fold_predictions_exp_date ON strategy_fold_predictions(experiment_id, test_date)",
    "CREATE INDEX IF NOT EXISTS idx_strategy_fold_predictions_strategy_date ON strategy_fold_predictions(strategy_name, test_date DESC)",
    "CREATE INDEX IF NOT EXISTS idx_daily_portfolio_baselines_strategy_date ON daily_portfolio_baselines(strategy_name, signal_date DESC)",
    "CREATE INDEX IF NOT EXISTS idx_daily_portfolio_baselines_id_date ON daily_portfolio_baselines(portfolio_baseline_id, signal_date DESC)",
    "CREATE INDEX IF NOT EXISTS idx_strategy_outcomes_baseline ON strategy_outcomes(validation_baseline_id)",
    "CREATE INDEX IF NOT EXISTS idx_strategy_tuning_runs_strategy_time ON strategy_tuning_runs(strategy_name, run_time DESC)",
    "CREATE INDEX IF NOT EXISTS idx_stock_prediction_snapshots_date ON stock_prediction_snapshots(prediction_date DESC)",
    "CREATE INDEX IF NOT EXISTS idx_stock_prediction_snapshots_code ON stock_prediction_snapshots(code)",
)

_QUERY_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_strategy_signals_code_date ON strategy_signals(code, signal_date)",
    "CREATE INDEX IF NOT EXISTS idx_strategy_outcomes_code_trade_date ON strategy_outcomes(code, next_trade_date)",
)

_SNAPSHOT_PHASE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_signal_batches_phase_date ON strategy_signal_batches(strategy_name, snapshot_phase, signal_date DESC)",
    "CREATE INDEX IF NOT EXISTS idx_signals_phase_date ON strategy_signals(strategy_name, snapshot_phase, signal_date DESC, rank)",
    "CREATE INDEX IF NOT EXISTS idx_candidates_phase_date ON strategy_candidate_snapshots(strategy_name, snapshot_phase, signal_date DESC)",
)

_OUTCOME_MIGRATION_COLUMNS = {
    "overnight_return": "REAL NOT NULL DEFAULT 0",
    "signal_next_close_return": "REAL",
    "signal_intraday_high_return": "REAL",
    "signal_hold_3d_return": "REAL",
    "future_days": "INTEGER",
    "hold_5d_return": "REAL",
    "hold_10d_return": "REAL",
    "hold_20d_return": "REAL",
    "signal_hold_5d_return": "REAL",
    "signal_hold_10d_return": "REAL",
    "signal_hold_20d_return": "REAL",
    "signal_max_gain_3d": "REAL",
    "signal_max_drawdown_3d": "REAL",
    "signal_hit_3pct": "INTEGER",
    "signal_hit_5pct": "INTEGER",
    "exit_return": "REAL",
    "signal_exit_return": "REAL",
    "exit_reason": "TEXT",
    "exit_days": "INTEGER",
    "exit_date": "TEXT",
    "survivorship_corrected": "INTEGER NOT NULL DEFAULT 0",
    "correction_reason": "TEXT NOT NULL DEFAULT ''",
    "trade_cost_pct": "REAL NOT NULL DEFAULT 0",
    "primary_return_field": "TEXT NOT NULL DEFAULT ''",
    "primary_return": "REAL NOT NULL DEFAULT 0",
    "primary_return_net": "REAL NOT NULL DEFAULT 0",
    "primary_holding_days": "INTEGER NOT NULL DEFAULT 0",
    "validation_baseline_id": "TEXT NOT NULL DEFAULT ''",
    "validation_baseline_json": "TEXT NOT NULL DEFAULT ''",
    "label_status": "TEXT NOT NULL DEFAULT 'settled'",
    "delisting_status": "TEXT NOT NULL DEFAULT 'not_applicable'",
    "execution_policy_version": "TEXT NOT NULL DEFAULT ''",
    "execution_policy_json": "TEXT NOT NULL DEFAULT ''",
    "cost_scenarios_json": "TEXT NOT NULL DEFAULT '{}'",
    "raw_prices_json": "TEXT NOT NULL DEFAULT '[]'",
    "benchmark_json": "TEXT NOT NULL DEFAULT '{}'",
    "entry_price": "REAL",
    "exit_price": "REAL",
    "return_reproducible": "INTEGER NOT NULL DEFAULT 0",
    "position_status": "TEXT NOT NULL DEFAULT 'closed'",
    "entry_trade_date": "TEXT NOT NULL DEFAULT ''",
    "earliest_exit_date": "TEXT NOT NULL DEFAULT ''",
    "exit_trade_date": "TEXT NOT NULL DEFAULT ''",
    "price_adjustment_mode": "TEXT NOT NULL DEFAULT ''",
}

_BATCH_MIGRATION_COLUMNS = {
    "candidate_count": "INTEGER NOT NULL DEFAULT 0",
    "selected_count": "INTEGER NOT NULL DEFAULT 0",
    "data_source_timestamp": "TEXT NOT NULL DEFAULT ''",
    "market_data_cutoff": "TEXT NOT NULL DEFAULT ''",
    "execution_policy_version": "TEXT NOT NULL DEFAULT ''",
    "execution_policy_json": "TEXT NOT NULL DEFAULT ''",
    "generation_json": "TEXT NOT NULL DEFAULT '{}'",
    "portfolio_capital": "REAL NOT NULL DEFAULT 0",
    "snapshot_id": "TEXT NOT NULL DEFAULT ''",
    "sample_type": "TEXT NOT NULL DEFAULT 'unknown'",
    "sample_source": "TEXT NOT NULL DEFAULT ''",
    "snapshot_phase": "TEXT NOT NULL DEFAULT 'legacy_unknown'",
}

_CANDIDATE_MIGRATION_COLUMNS = {
    "snapshot_id": "TEXT NOT NULL DEFAULT ''",
    "sample_type": "TEXT NOT NULL DEFAULT 'unknown'",
    "sample_source": "TEXT NOT NULL DEFAULT ''",
    "snapshot_phase": "TEXT NOT NULL DEFAULT 'legacy_unknown'",
}

_SHADOW_OUTCOME_MIGRATION_COLUMNS = {
    "overnight_return": "REAL NOT NULL DEFAULT 0",
}

_EXECUTION_MIGRATION_COLUMNS = {
    "unfilled_entry_quantity": "REAL NOT NULL DEFAULT 0",
    "unfilled_exit_quantity": "REAL NOT NULL DEFAULT 0",
    "position_status": "TEXT NOT NULL DEFAULT 'not_entered'",
    "entry_trade_date": "TEXT NOT NULL DEFAULT ''",
    "earliest_exit_date": "TEXT NOT NULL DEFAULT ''",
    "exit_trade_date": "TEXT NOT NULL DEFAULT ''",
    "mark_price": "REAL",
    "price_adjustment_mode": "TEXT NOT NULL DEFAULT ''",
}

_PORTFOLIO_BASELINE_MIGRATION_COLUMNS = {
    "audit_blob": "BLOB",
}

_FOLD_PREDICTION_MIGRATION_COLUMNS = {
    "baseline_id": "TEXT NOT NULL DEFAULT ''",
    "model_id": "TEXT NOT NULL DEFAULT ''",
    "model_version": "TEXT NOT NULL DEFAULT ''",
    "train_end_date": "TEXT NOT NULL DEFAULT ''",
    "baseline_score": "REAL",
    "predicted_probability": "REAL",
    "feature_schema_hash": "TEXT NOT NULL DEFAULT ''",
}

_OOS_REPORT_MIGRATION_COLUMNS = {
    "experiment_audit_json": "TEXT NOT NULL DEFAULT '{}'",
}

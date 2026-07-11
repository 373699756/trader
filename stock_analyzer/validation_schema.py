from __future__ import annotations


class ValidationSchemaManager:
    """Owns SQLite schema creation and lightweight migrations."""

    def __init__(self, connect_fn, db_path: str) -> None:
        self._connect = connect_fn
        self.db_path = db_path

    def init_db(self) -> None:
        with self._connect(self.db_path) as conn:
            self._create_tables(conn)
            self._create_indexes(conn)
            self._run_migrations(conn)

    def _create_tables(self, conn) -> None:
        for statement in _TABLES:
            conn.execute(statement)

    def _create_indexes(self, conn) -> None:
        for statement in _INDEXES:
            conn.execute(statement)

    def _run_migrations(self, conn) -> None:
        existing_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(strategy_outcomes)").fetchall()
        }
        for column, column_type in _OUTCOME_MIGRATION_COLUMNS.items():
            if column not in existing_columns:
                conn.execute("ALTER TABLE strategy_outcomes ADD COLUMN {} {}".format(column, column_type))
        conn.execute(
            """
            INSERT OR IGNORE INTO strategy_signal_batches
            (strategy_name, strategy_version, signal_date, signal_time, saved_count, created_at)
            SELECT strategy_name, strategy_version, signal_date, MAX(signal_time), COUNT(*), MIN(created_at)
            FROM strategy_signals
            GROUP BY strategy_name, strategy_version, signal_date
            """
        )


_TABLES = (
    """
    CREATE TABLE IF NOT EXISTS strategy_signal_batches (
        strategy_name TEXT NOT NULL,
        strategy_version TEXT NOT NULL,
        signal_date TEXT NOT NULL,
        signal_time TEXT NOT NULL,
        saved_count INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        PRIMARY KEY(strategy_name, signal_date, strategy_version)
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
        created_at TEXT NOT NULL,
        UNIQUE(strategy_name, strategy_version, signal_date, code)
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
        created_at TEXT NOT NULL,
        UNIQUE(strategy_name, strategy_version, signal_date, code)
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
    CREATE TABLE IF NOT EXISTS deepseek_market_gate_reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        review_date TEXT NOT NULL,
        review_time TEXT NOT NULL,
        market_filter TEXT NOT NULL DEFAULT 'all',
        regime TEXT NOT NULL DEFAULT '',
        size_factor REAL NOT NULL DEFAULT 1,
        confidence REAL NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL DEFAULT '',
        context_json TEXT NOT NULL,
        result_json TEXT NOT NULL,
        counts_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(review_date, market_filter)
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
        report_json TEXT NOT NULL,
        baseline_status_json TEXT NOT NULL,
        validation_gate_json TEXT NOT NULL,
        requirements_json TEXT NOT NULL,
        created_at TEXT NOT NULL
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

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_strategy_signals_date ON strategy_signals(signal_date)",
    "CREATE INDEX IF NOT EXISTS idx_strategy_signals_strategy_date ON strategy_signals(strategy_name, signal_date DESC)",
    "CREATE INDEX IF NOT EXISTS idx_strategy_signals_strategy_date_rank ON strategy_signals(strategy_name, signal_date DESC, rank)",
    "CREATE INDEX IF NOT EXISTS idx_strategy_signals_strategy_version_date ON strategy_signals(strategy_name, strategy_version, signal_date)",
    "CREATE INDEX IF NOT EXISTS idx_strategy_signal_batches_strategy_date ON strategy_signal_batches(strategy_name, signal_date DESC)",
    "CREATE INDEX IF NOT EXISTS idx_strategy_outcomes_code ON strategy_outcomes(code)",
    "CREATE INDEX IF NOT EXISTS idx_strategy_execution_skips_code ON strategy_execution_skips(code)",
    "CREATE INDEX IF NOT EXISTS idx_deepseek_shadow_strategy_date ON strategy_deepseek_shadow_signals(strategy_name, signal_date DESC)",
    "CREATE INDEX IF NOT EXISTS idx_deepseek_shadow_code ON strategy_deepseek_shadow_signals(code)",
    "CREATE INDEX IF NOT EXISTS idx_deepseek_market_gate_date ON deepseek_market_gate_reviews(review_date DESC)",
    "CREATE INDEX IF NOT EXISTS idx_strategy_oos_reports_strategy_time ON strategy_oos_reports(strategy_name, generated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_strategy_outcomes_baseline ON strategy_outcomes(validation_baseline_id)",
    "CREATE INDEX IF NOT EXISTS idx_strategy_tuning_runs_strategy_time ON strategy_tuning_runs(strategy_name, run_time DESC)",
    "CREATE INDEX IF NOT EXISTS idx_stock_prediction_snapshots_date ON stock_prediction_snapshots(prediction_date DESC)",
    "CREATE INDEX IF NOT EXISTS idx_stock_prediction_snapshots_code ON stock_prediction_snapshots(code)",
)

_OUTCOME_MIGRATION_COLUMNS = {
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
}

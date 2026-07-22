"""SQLite helpers shared by DeepSeek budget operations."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from trader.domain.recommendation.models import Strategy


def _stage_key(strategy: Strategy, phase: str, bucket: str) -> str:
    if bucket == "shared_preheat":
        return "shared_preheat"
    if bucket == "emergency":
        return "emergency"
    if strategy is Strategy.TODAY and phase in {"today_observe", "today_main", "today_late"}:
        return phase
    if strategy is Strategy.TOMORROW and phase in {"afternoon", "final_review"}:
        return "tomorrow_final" if phase == "final_review" else "tomorrow_afternoon"
    if strategy is Strategy.D25 and phase in {"afternoon", "final_review"}:
        return "d25_final" if phase == "final_review" else "d25_afternoon"
    if strategy is Strategy.LONG and phase in {"afternoon", "final_review"}:
        return "long_afternoon"
    return f"{strategy.value}_{phase}"


def _count(connection: sqlite3.Connection, where: str, parameters: tuple[object, ...]) -> int:
    return int(
        connection.execute(
            f"SELECT COUNT(*) FROM deepseek_call_reservations WHERE {where}",
            parameters,
        ).fetchone()[0]
    )


def _ensure_column(connection: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
    columns = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def _current_schema_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        return 0
    try:
        return int(str(row[0]))
    except (TypeError, ValueError):
        return 0


def _sync_call_audit(connection: sqlite3.Connection, reservation_id: str) -> None:
    connection.execute(
        """
        INSERT INTO deepseek_calls(
            call_id, strategy, phase, model, batch_id, requested_at, completed_at,
            http_status, prompt_tokens, completion_tokens, latency_ms, outcome, error_code,
            model_role, requested_model, actual_model, reasoning_effort, system_fingerprint,
            finish_reason, total_tokens, prompt_cache_hit_tokens, prompt_cache_miss_tokens
        )
        SELECT
            r.reservation_id, r.strategy, r.phase, COALESCE(r.requested_model, b.model, ''), r.batch_id,
            r.requested_at, r.completed_at, r.http_status, r.prompt_tokens, r.completion_tokens, r.latency_ms,
            r.status,
            CASE
                WHEN r.timed_out = 1 THEN 'timeout'
                WHEN r.status = 'abandoned' THEN 'abandoned'
                WHEN r.http_status IS NOT NULL AND r.status = 'failed' THEN 'http_' || r.http_status
                WHEN r.status = 'failed' THEN 'request_failed'
                ELSE ''
            END,
            r.model_role, r.requested_model, r.actual_model, r.reasoning_effort, r.system_fingerprint,
            r.finish_reason, r.token_count,
            r.prompt_cache_hit_tokens, r.prompt_cache_miss_tokens
        FROM deepseek_call_reservations AS r
        LEFT JOIN deepseek_review_batches AS b ON b.batch_id = r.batch_id
        WHERE r.reservation_id = ?
        ON CONFLICT(call_id) DO UPDATE SET
            completed_at = excluded.completed_at,
            http_status = excluded.http_status,
            completion_tokens = excluded.completion_tokens,
            prompt_tokens = excluded.prompt_tokens,
            latency_ms = excluded.latency_ms,
            outcome = excluded.outcome,
            error_code = excluded.error_code
            ,model_role = excluded.model_role
            ,requested_model = excluded.requested_model
            ,actual_model = excluded.actual_model
            ,reasoning_effort = excluded.reasoning_effort
            ,system_fingerprint = excluded.system_fingerprint
            ,finish_reason = excluded.finish_reason
            ,total_tokens = excluded.total_tokens
            ,prompt_cache_hit_tokens = excluded.prompt_cache_hit_tokens
            ,prompt_cache_miss_tokens = excluded.prompt_cache_miss_tokens
        """,
        (reservation_id,),
    )
    connection.execute(
        """
        DELETE FROM deepseek_calls
        WHERE call_id IN (
            SELECT call_id FROM deepseek_calls
            ORDER BY requested_at DESC
            LIMIT -1 OFFSET 10000
        )
        """
    )


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")

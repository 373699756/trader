"""Persisted DeepSeek batch and candidate terminal-state operations."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime

from trader.domain.recommendation.models import Strategy
from trader.domain.review.models import DeepSeekReview
from trader.infra.deepseek.budget_state import BudgetStoreState
from trader.infra.deepseek.budget_support import _require_aware, _sync_call_audit

_BATCH_TERMINALS = frozenset({"success", "partial", "failed", "skipped", "abandoned"})
_CANDIDATE_TERMINALS = frozenset({"applied", "abstain", "rejected", "late"})


class BudgetBatchMixin(BudgetStoreState):
    def begin_batch(
        self,
        strategy: Strategy,
        *,
        phase: str,
        bucket: str,
        model: str,
        requested_at: datetime,
        deadline: datetime,
        candidate_codes: Sequence[str],
        cache_hit_count: int = 0,
    ) -> str:
        _require_aware(requested_at, "batch requested_at")
        _require_aware(deadline, "batch deadline")
        codes = tuple(candidate_codes)
        if len(codes) != len(set(codes)):
            raise ValueError("DeepSeek batch candidate codes must be unique")
        if cache_hit_count < 0 or cache_hit_count > len(codes):
            raise ValueError("DeepSeek batch cache hits must be within candidate count")
        batch_id = uuid.uuid4().hex
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO deepseek_review_batches(
                    batch_id, trade_date, strategy, phase, bucket, model, requested_at,
                    deadline, status, candidate_count, cache_hit_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?)
                """,
                (
                    batch_id,
                    requested_at.date().isoformat(),
                    strategy.value,
                    phase,
                    bucket,
                    model,
                    requested_at.isoformat(),
                    deadline.isoformat(),
                    len(codes),
                    cache_hit_count,
                ),
            )
            connection.executemany(
                """
                INSERT INTO deepseek_candidate_results(batch_id, stock_code, outcome)
                VALUES (?, ?, 'pending')
                """,
                ((batch_id, code) for code in codes),
            )
            connection.commit()
        return batch_id

    def finish_batch(
        self,
        batch_id: str,
        *,
        status: str,
        completed_at: datetime,
        reviews: Mapping[str, DeepSeekReview],
        physical_attempts: int,
        error: str = "",
    ) -> None:
        if status not in _BATCH_TERMINALS - {"abandoned"}:
            raise ValueError(f"invalid review batch terminal status: {status}")
        _require_aware(completed_at, "batch completed_at")
        if physical_attempts < 0:
            raise ValueError("physical attempts cannot be negative")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """
                UPDATE deepseek_review_batches
                SET completed_at = ?, status = ?, physical_attempts = ?, error = ?
                WHERE batch_id = ? AND status = 'running'
                """,
                (completed_at.isoformat(), status, physical_attempts, error[:1000], batch_id),
            ).rowcount
            if changed != 1:
                connection.rollback()
                raise KeyError(f"unknown or completed DeepSeek batch: {batch_id}")
            for code, review in reviews.items():
                outcome = review.outcome.value
                if code != review.code or outcome not in _CANDIDATE_TERMINALS:
                    connection.rollback()
                    raise ValueError("DeepSeek candidate result does not match its batch code")
                updated = connection.execute(
                    """
                    UPDATE deepseek_candidate_results
                    SET outcome = ?, completed_at = ?, error = ?
                    WHERE batch_id = ? AND stock_code = ? AND outcome = 'pending'
                    """,
                    (outcome, review.completed_at.isoformat(), review.error[:500], batch_id, code),
                ).rowcount
                if updated != 1:
                    connection.rollback()
                    raise ValueError(f"candidate {code} is not pending in batch {batch_id}")
            connection.execute(
                """
                UPDATE deepseek_candidate_results
                SET outcome = 'rejected', completed_at = ?, error = ?
                WHERE batch_id = ? AND outcome = 'pending'
                """,
                (completed_at.isoformat(), f"batch_{status}", batch_id),
            )
            connection.commit()

    def set_batch_cache_hits(self, batch_id: str, count: int) -> None:
        if count < 0:
            raise ValueError("batch cache hits cannot be negative")
        with self._connect() as connection:
            changed = connection.execute(
                """
                UPDATE deepseek_review_batches
                SET cache_hit_count = ?
                WHERE batch_id = ? AND status = 'running' AND candidate_count >= ?
                """,
                (count, batch_id, count),
            ).rowcount
            if changed != 1:
                raise KeyError(f"unknown batch or invalid cache hit count: {batch_id}")

    def fail_running_batch(self, batch_id: str, *, completed_at: datetime, error: str) -> bool:
        _require_aware(completed_at, "batch failure time")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """
                UPDATE deepseek_review_batches
                SET completed_at = ?, status = 'failed', error = ?
                WHERE batch_id = ? AND status = 'running'
                """,
                (completed_at.isoformat(), error[:1000], batch_id),
            ).rowcount
            if changed:
                reservation_ids = tuple(
                    str(row[0])
                    for row in connection.execute(
                        "SELECT reservation_id FROM deepseek_call_reservations WHERE batch_id = ? AND status = 'reserved'",
                        (batch_id,),
                    ).fetchall()
                )
                connection.execute(
                    """
                    UPDATE deepseek_candidate_results
                    SET outcome = 'rejected', completed_at = ?, error = 'batch_failed'
                    WHERE batch_id = ? AND outcome = 'pending'
                    """,
                    (completed_at.isoformat(), batch_id),
                )
                connection.execute(
                    """
                    UPDATE deepseek_call_reservations
                    SET status = 'abandoned', completed_at = ?, error = ?
                    WHERE batch_id = ? AND status = 'reserved'
                    """,
                    (completed_at.isoformat(), error[:1000], batch_id),
                )
                for reservation_id in reservation_ids:
                    _sync_call_audit(connection, reservation_id)
            connection.commit()
        return bool(changed)

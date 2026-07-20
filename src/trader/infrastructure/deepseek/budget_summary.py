"""Read-only DeepSeek budget and outcome aggregation."""

from __future__ import annotations

from trader.domain.models import Strategy
from trader.infrastructure.deepseek.budget_state import BudgetStoreState

_BATCH_TERMINALS = frozenset({"success", "partial", "failed", "skipped", "abandoned"})
_CALL_TERMINALS = frozenset({"success", "failed", "abandoned"})
_CANDIDATE_TERMINALS = frozenset({"applied", "abstain", "rejected", "late"})


class BudgetSummaryMixin(BudgetStoreState):
    def summary(self, day: str) -> dict[str, object]:
        if not self._initialized:
            return {
                "used": 0,
                "remaining": self._daily_hard_limit,
                "target": self._daily_target,
                "target_met": False,
                "by_bucket": {},
                "by_strategy": {},
                "by_stage": {},
                "by_status": {},
                "call_status": {name: 0 for name in ("reserved", *sorted(_CALL_TERMINALS))},
                "batch_status": {name: 0 for name in sorted(_BATCH_TERMINALS)},
                "candidate_outcomes": {name: 0 for name in sorted(_CANDIDATE_TERMINALS)},
                "by_model_role": {"primary": 0, "challenger": 0},
                "http_429_count": 0,
                "timeout_count": 0,
                "token_count": 0,
            }
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT bucket, strategy, stage_key, status, COUNT(*)
                FROM deepseek_call_reservations
                WHERE trade_date = ?
                GROUP BY bucket, strategy, stage_key, status
                """,
                (day,),
            ).fetchall()
            batch_rows = connection.execute(
                """
                SELECT status, COUNT(*) FROM deepseek_review_batches
                WHERE trade_date = ? GROUP BY status
                """,
                (day,),
            ).fetchall()
            candidate_rows = connection.execute(
                """
                SELECT r.outcome, COUNT(*)
                FROM deepseek_candidate_results AS r
                JOIN deepseek_review_batches AS b ON b.batch_id = r.batch_id
                WHERE b.trade_date = ? GROUP BY r.outcome
                """,
                (day,),
            ).fetchall()
            acceptance_row = connection.execute(
                """
                SELECT
                    SUM(CASE WHEN http_status = 429 THEN 1 ELSE 0 END),
                    SUM(CASE WHEN timed_out = 1 THEN 1 ELSE 0 END),
                    SUM(token_count)
                FROM deepseek_call_reservations WHERE trade_date = ?
                """,
                (day,),
            ).fetchone()
            role_rows = connection.execute(
                "SELECT model_role, COUNT(*) FROM deepseek_call_reservations WHERE trade_date = ? GROUP BY model_role",
                (day,),
            ).fetchall()
        by_bucket: dict[str, int] = {}
        by_strategy: dict[str, int] = {}
        by_stage_count: dict[str, int] = {}
        by_status: dict[str, int] = {}
        for bucket, strategy, stage, status, count in rows:
            amount = int(count)
            by_bucket[str(bucket)] = by_bucket.get(str(bucket), 0) + amount
            by_stage_count[str(stage)] = by_stage_count.get(str(stage), 0) + amount
            by_status[str(status)] = by_status.get(str(status), 0) + amount
            if str(bucket) in {item.value for item in Strategy} or str(bucket) == "emergency":
                by_strategy[str(strategy)] = by_strategy.get(str(strategy), 0) + amount
        used = sum(by_bucket.values())
        by_stage = {
            stage: {
                "used": by_stage_count.get(stage, 0),
                "target": self._stage_targets[stage],
                "limit": self._stage_limits[stage],
                "remaining": max(0, self._stage_limits[stage] - by_stage_count.get(stage, 0)),
                "target_met": by_stage_count.get(stage, 0) >= self._stage_targets[stage],
            }
            for stage in self._stage_limits
        }
        target_met = all(
            by_stage_count.get(stage, 0) >= target
            for stage, target in self._stage_targets.items()
            if stage != "emergency"
        )
        return {
            "used": used,
            "remaining": max(0, self._daily_hard_limit - used),
            "target": self._daily_target,
            "target_met": target_met,
            "by_bucket": by_bucket,
            "by_strategy": by_strategy,
            "by_stage": by_stage,
            "by_status": by_status,
            "call_status": {name: by_status.get(name, 0) for name in ("reserved", *sorted(_CALL_TERMINALS))},
            "batch_status": {
                name: dict((str(status), int(count)) for status, count in batch_rows).get(name, 0)
                for name in sorted(_BATCH_TERMINALS)
            },
            "candidate_outcomes": {
                name: dict((str(outcome), int(count)) for outcome, count in candidate_rows).get(name, 0)
                for name in sorted(_CANDIDATE_TERMINALS)
            },
            "by_model_role": {
                role: dict((str(name), int(count)) for name, count in role_rows).get(role, 0)
                for role in ("primary", "challenger")
            },
            "http_429_count": int(acceptance_row[0] or 0),
            "timeout_count": int(acceptance_row[1] or 0),
            "token_count": int(acceptance_row[2] or 0),
        }

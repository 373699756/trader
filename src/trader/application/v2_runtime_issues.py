"""Bounded issue registry for the unified V2 scheduler."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal

from trader.domain.recommendation.models import Strategy

_ISSUE_HISTORY_CAPACITY = 20


@dataclass(frozen=True)
class V2RuntimeIssue:
    code: str
    severity: Literal["degraded", "error"]
    strategy: Strategy | None
    stage: str
    occurred_at: datetime
    last_occurred_at: datetime
    count: int
    recovery_status: Literal["active", "recovered"]
    resolved_at: datetime | None


@dataclass(frozen=True)
class V2RuntimeIssueSnapshot:
    last_error_code: str
    strategy_error_codes: tuple[tuple[str, str], ...]
    recent_errors: tuple[V2RuntimeIssue, ...]


class V2RuntimeIssueRegistry:
    """Own issue identity, bounded history, recovery and observable ordering."""

    def __init__(self) -> None:
        self._system_error_code = ""
        self._strategy_error_codes: dict[Strategy, str] = {}
        self._recent_errors: OrderedDict[tuple[str, str], V2RuntimeIssue] = OrderedDict()

    def record(self, code: str, stage: str, strategy: Strategy | None, occurred_at: datetime) -> None:
        key = (strategy.value if strategy is not None else "system", code)
        existing = self._recent_errors.pop(key, None)
        self._recent_errors[key] = V2RuntimeIssue(
            code=code,
            severity="error" if stage in {"freeze", "settlement", "publish"} else "degraded",
            strategy=strategy,
            stage=stage,
            occurred_at=existing.occurred_at if existing is not None else occurred_at,
            last_occurred_at=occurred_at,
            count=existing.count + 1 if existing is not None else 1,
            recovery_status="active",
            resolved_at=None,
        )
        while len(self._recent_errors) > _ISSUE_HISTORY_CAPACITY:
            self._recent_errors.popitem(last=False)
        if strategy is None:
            self._system_error_code = code
        else:
            self._strategy_error_codes[strategy] = code

    def resolve(
        self,
        resolved_at: datetime,
        *,
        strategy: Strategy | None = None,
        stages: frozenset[str] | None = None,
    ) -> None:
        for key, issue in tuple(self._recent_errors.items()):
            if issue.recovery_status != "active":
                continue
            if strategy is not None and issue.strategy is not strategy:
                continue
            if stages is not None and issue.stage not in stages:
                continue
            self._recent_errors[key] = replace(issue, recovery_status="recovered", resolved_at=resolved_at)
        if strategy is None and stages is not None:
            self._system_error_code = self._latest_active_code(None)
        elif strategy is not None:
            active_code = self._latest_active_code(strategy)
            if active_code:
                self._strategy_error_codes[strategy] = active_code
            else:
                self._strategy_error_codes.pop(strategy, None)

    def snapshot(self) -> V2RuntimeIssueSnapshot:
        last_error_code = self._system_error_code or next(reversed(self._strategy_error_codes.values()), "")
        return V2RuntimeIssueSnapshot(
            last_error_code=last_error_code,
            strategy_error_codes=tuple(
                (strategy.value, self._strategy_error_codes[strategy])
                for strategy in Strategy
                if strategy in self._strategy_error_codes
            ),
            recent_errors=tuple(
                sorted(
                    reversed(self._recent_errors.values()),
                    key=lambda issue: (
                        0 if issue.severity == "error" else 1,
                        0 if issue.recovery_status == "active" else 1,
                        -issue.last_occurred_at.timestamp(),
                    ),
                )
            ),
        )

    def _latest_active_code(self, strategy: Strategy | None) -> str:
        for issue in reversed(self._recent_errors.values()):
            if issue.strategy is strategy and issue.recovery_status == "active":
                return issue.code
        return ""


__all__ = ["V2RuntimeIssue", "V2RuntimeIssueRegistry", "V2RuntimeIssueSnapshot"]

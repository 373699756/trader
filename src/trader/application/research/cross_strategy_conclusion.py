"""Read-only, hash-bound conclusion for the Today/Tomorrow/D25 terminal reports."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from trader.application.research.replay_models import canonical_hash
from trader.domain.research.terminal_holdout import TerminalHoldoutMetrics, TerminalHoldoutReport, TerminalStatus

CrossStrategyStatus = Literal["historical_data_insufficient", "historical_rejected", "historical_validated"]


@dataclass(frozen=True)
class CrossStrategyConclusion:
    today: TerminalHoldoutReport
    tomorrow: TerminalHoldoutReport
    d25: TerminalHoldoutReport
    status: CrossStrategyStatus
    report_hashes: tuple[tuple[str, str], ...]
    production_authority: bool = False
    schema_version: str = "historical_cross_strategy_conclusion_v1"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        reports = (self.today, self.tomorrow, self.d25)
        if tuple(report.strategy for report in reports) != ("today", "tomorrow", "d25"):
            raise ValueError("cross-strategy conclusion requires reports in fixed strategy order")
        expected = tuple((report.strategy, report.content_hash) for report in reports)
        if self.report_hashes != expected:
            raise ValueError("cross-strategy conclusion must bind each report hash")
        if self.status not in {"historical_data_insufficient", "historical_rejected", "historical_validated"}:
            raise ValueError("cross-strategy conclusion status is invalid")
        if self.production_authority:
            raise ValueError("cross-strategy conclusion cannot authorize production")
        object.__setattr__(self, "content_hash", canonical_hash(self))

    @property
    def strategy_statuses(self) -> tuple[tuple[str, TerminalStatus], ...]:
        return tuple((report.strategy, report.status) for report in (self.today, self.tomorrow, self.d25))

    @property
    def strategy_metrics(self) -> tuple[tuple[str, TerminalHoldoutMetrics], ...]:
        """Expose each strategy's metrics without merging failures or row counts."""

        return tuple((report.strategy, report.metrics) for report in (self.today, self.tomorrow, self.d25))


class CrossStrategyConclusionService:
    """Combine sealed reports without averaging away a failed strategy."""

    def execute(
        self,
        today: TerminalHoldoutReport,
        tomorrow: TerminalHoldoutReport,
        d25: TerminalHoldoutReport,
    ) -> CrossStrategyConclusion:
        reports = (today, tomorrow, d25)
        if any(report.production_authority for report in reports):
            raise ValueError("cross-strategy conclusion accepts research-only reports")
        statuses = tuple(report.status for report in reports)
        if all(status == "historical_validated" for status in statuses):
            status: CrossStrategyStatus = "historical_validated"
        elif "historical_data_insufficient" in statuses:
            status = "historical_data_insufficient"
        else:
            status = "historical_rejected"
        return CrossStrategyConclusion(
            today=today,
            tomorrow=tomorrow,
            d25=d25,
            status=status,
            report_hashes=tuple((report.strategy, report.content_hash) for report in reports),
        )


__all__ = ["CrossStrategyConclusion", "CrossStrategyConclusionService", "CrossStrategyStatus"]

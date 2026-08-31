"""After-close T+1 settlement for all formally matched V1/V2 candidate pairs."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from trader.application.outcome_settlement import OutcomeSettlementMarketData
from trader.application.ports.decision_records import DecisionRecordRepositoryPort
from trader.application.ports.outcomes import OutcomeTargetReaderPort
from trader.application.ports.tomorrow_profile_comparison import TomorrowProfileEvidencePort
from trader.application.runtime.schedule import shanghai_now
from trader.application.tomorrow_profile_reporting import TomorrowProfileReportingService
from trader.domain.market.models import FeatureSnapshot
from trader.domain.outcome.evaluation import OutcomeEvaluationRequest, evaluate_outcome
from trader.domain.outcome.models import OutcomeTarget
from trader.domain.recommendation.models import Strategy


@dataclass(frozen=True)
class TomorrowProfileSettlementResult:
    formal_bound_count: int
    target_count: int
    outcome_count: int
    complete_count: int


@dataclass(frozen=True)
class TomorrowProfileSettlementDependencies:
    market_data: OutcomeSettlementMarketData
    decisions: DecisionRecordRepositoryPort
    benchmarks: OutcomeTargetReaderPort
    evidence: TomorrowProfileEvidencePort


class TomorrowProfileSettlementService:
    def __init__(
        self,
        dependencies: TomorrowProfileSettlementDependencies,
        *,
        session_distance: Callable[[str, str], int | None],
        target_limit: int = 2_000,
        reporting: TomorrowProfileReportingService | None = None,
    ) -> None:
        self._market_data = dependencies.market_data
        self._decisions = dependencies.decisions
        self._benchmarks = dependencies.benchmarks
        self._evidence = dependencies.evidence
        self._session_distance = session_distance
        self._target_limit = target_limit
        self._reporting = reporting

    def settle(
        self,
        now: datetime,
        market_features: Sequence[FeatureSnapshot],
    ) -> TomorrowProfileSettlementResult:
        del market_features
        local = shanghai_now(now)
        formal_bound = self._bind_formal_inputs()
        targets = tuple(self._evidence.pending_formal_targets(limit=self._target_limit))
        if not targets:
            self._seal_terminal_if_ready()
            return TomorrowProfileSettlementResult(formal_bound, 0, 0, 0)
        codes = tuple(dict.fromkeys(item.pair.code for item in targets))
        histories = self._market_data.read_outcome_bars(codes, now)
        outcomes = []
        current_date = local.date().isoformat()
        for target in targets:
            recommend_date = target.trade_date.isoformat()
            elapsed = self._session_distance(recommend_date, current_date)
            if elapsed is None or elapsed < 1:
                continue
            loaded = tuple(self._benchmarks.benchmark_returns_after(recommend_date, limit=1))
            benchmarks = tuple(item for item in loaded if self._session_distance(recommend_date, item.trade_date) == 1)
            expected_dates = tuple(item.trade_date for item in benchmarks)
            bars = tuple(
                bar for bar in histories.get(target.pair.code, ()) if recommend_date <= bar.trade_date <= current_date
            )
            outcomes.append(
                evaluate_outcome(
                    OutcomeEvaluationRequest(
                        target=OutcomeTarget(
                            target.input_version,
                            Strategy.TOMORROW,
                            recommend_date,
                            target.pair.code,
                            target.pair.anchor_price,
                            target.pair.atr20_pct if target.pair.atr20_pct is not None else 0.0,
                        ),
                        bars=bars,
                        horizon=1,
                        benchmark_returns=tuple(item.return_pct for item in benchmarks),
                        expected_sessions=1,
                        expected_trade_dates=expected_dates,
                        settled_at=now,
                    )
                )
            )
        if outcomes:
            self._evidence.save_outcomes(outcomes)
        self._seal_terminal_if_ready()
        return TomorrowProfileSettlementResult(
            formal_bound,
            len(targets),
            len(outcomes),
            sum(item.status == "complete" for item in outcomes),
        )

    def _seal_terminal_if_ready(self) -> None:
        if self._reporting is not None and self._evidence.status().state == "power_ready":
            report = self._reporting.report()
            if report.state in {"review_ready", "rejected"}:
                self._evidence.save_terminal_report(report)

    def _bind_formal_inputs(self) -> int:
        bound = 0
        for trade_date in reversed(self._decisions.list_dates(Strategy.TOMORROW, limit=31)):
            record = self._decisions.load(Strategy.TOMORROW, trade_date)
            if record is None:
                continue
            input_version = dict(record.decision.input_versions).get("native")
            if input_version is None or self._evidence.load_manifest(input_version) is None:
                continue
            self._evidence.bind_formal_input(
                trade_date=trade_date,
                input_version=input_version,
                record_version=record.version,
                committed_at=record.committed_at,
            )
            bound += 1
        return bound


__all__ = [
    "TomorrowProfileSettlementDependencies",
    "TomorrowProfileSettlementResult",
    "TomorrowProfileSettlementService",
]

"""Company-research triggers and result coordination for the recommendation pipeline."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from trader.application.cadence import PipelineTask
from trader.application.events import EventPriority, EventSpec, new_event
from trader.application.pipeline_state import PipelineState
from trader.application.ports.market import ResearchRefreshResult
from trader.application.schedule import MarketPhase, decision_at, trade_date_at
from trader.domain.recommendation.models import Strategy


class PipelineResearchMixin(PipelineState):
    def _company_research_codes(self, observed_at: datetime) -> tuple[str, ...]:
        trade_date = trade_date_at(observed_at).isoformat()
        snapshot_codes = (
            item.features.quote.code
            for strategy in (Strategy.TOMORROW, Strategy.D25, Strategy.TODAY)
            if (snapshot := self._state.latest(strategy)) is not None and snapshot.trade_date == trade_date
            for item in snapshot.recommendations
        )
        return tuple(dict.fromkeys((*snapshot_codes, *self._candidate_codes)))

    def _offer_company_research(
        self,
        observed_at: datetime,
        codes: Sequence[str] | None = None,
    ) -> bool:
        selected = tuple(codes) if codes is not None else self._company_research_codes(observed_at)
        accepted = self._research_coordinator.offer(selected, observed_at)
        if accepted:
            self._state.increment("company_research_offers")
        return accepted

    def _offer_new_recommendation_research(self, observed_at: datetime) -> bool:
        trade_date = trade_date_at(observed_at).isoformat()
        current_codes = tuple(
            dict.fromkeys(
                item.features.quote.code
                for strategy in (Strategy.TOMORROW, Strategy.D25, Strategy.TODAY)
                if (snapshot := self._state.latest(strategy)) is not None
                and snapshot.trade_date == trade_date
                and not snapshot.frozen
                for item in snapshot.recommendations
            )
        )
        with self._company_research_membership_lock:
            if self._company_research_membership_date != trade_date:
                self._company_research_membership_date = trade_date
                self._company_research_membership.clear()
            newly_entered = tuple(code for code in current_codes if code not in self._company_research_membership)
            self._company_research_membership = set(current_codes)
            needs_review_barrier = bool(newly_entered) and self._decision_execution_mode == "versioned_dag"
            if needs_review_barrier:
                self._company_research_review_barrier = True
                self._company_research_initial_rescore_pending = True
        accepted = bool(newly_entered) and self._offer_company_research(observed_at, newly_entered)
        if needs_review_barrier and not accepted:
            with self._company_research_membership_lock:
                self._company_research_review_barrier = False
                self._company_research_initial_rescore_pending = False
        return accepted

    def _consume_company_research_review_barrier(self) -> bool:
        with self._company_research_membership_lock:
            deferred = self._company_research_review_barrier
            self._company_research_review_barrier = False
            return deferred

    def _consume_company_research_initial_rescore(self) -> bool:
        with self._company_research_membership_lock:
            required = self._company_research_initial_rescore_pending
            self._company_research_initial_rescore_pending = False
            return required

    def _await_company_research(self, timeout_seconds: float) -> bool:
        completed = self._research_coordinator.wait_until_idle(timeout_seconds)
        if not completed:
            self._state.increment("company_research_close_wait_timeouts")
        return completed

    def _on_company_research_result(self, result: ResearchRefreshResult) -> None:
        self._state.increment("company_research_batches")
        self._state.increment("company_research_completed_codes", len(result.completed_codes))
        self._state.increment("company_research_deferred_codes", len(result.deferred_codes))
        self._state.increment("company_research_failed_codes", len(result.failed_codes))
        initial_rescore = self._consume_company_research_initial_rescore()
        if self._decision_execution_mode != "versioned_dag" or (not result.changed_codes and not initial_rescore):
            return
        completed_at = result.completed_at or self._now()
        session = self._refresh_trading_session(completed_at)
        phase = decision_at(
            completed_at,
            is_trading_day=session.is_trading_day is True,
        ).phase
        if phase is MarketPhase.CLOSED:
            return
        event = new_event(
            EventSpec(
                event_type=PipelineTask.SCORE.value,
                subject_key="market",
                trade_date=trade_date_at(completed_at).isoformat(),
                phase=phase.value,
                strategy=None,
                priority=EventPriority.RISK,
                data_version=f"stock_risk:{result.data_version}",
                config_version=self._config_version,
                created_at=completed_at,
                deadline=completed_at + timedelta(seconds=38.0),
                latest_wins=True,
                payload={
                    "schedule_task": PipelineTask.SCORE.value,
                    "trigger_event_type": PipelineTask.STOCK_RISK.value,
                    "session_generation": session.generation,
                    "session_trade_date": session.trade_date,
                },
            )
        )
        if self.submit_event(event):
            self._state.increment("triggered_scores_submitted")
        else:
            self._state.increment("triggered_scores_dropped")


__all__ = ["PipelineResearchMixin"]

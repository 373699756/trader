from __future__ import annotations

import threading
from datetime import datetime, timedelta

from trader.application.cadence import CadencePolicy
from trader.application.ports.market import ResearchRefreshResult
from trader.application.ports.v2_runtime import V2CycleRequest, V2ResearchIntent
from trader.application.schedule import SHANGHAI, MarketPhase
from trader.application.v2_research_runtime import V2ResearchRuntime
from trader.domain.recommendation.models import Strategy

NOW = datetime(2026, 8, 21, 10, 0, tzinfo=SHANGHAI)


class _Research:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.started = threading.Event()
        self.release = threading.Event()

    def refresh_stock_risk(self, codes, observed_at, *, deadline=None):
        del deadline
        batch = tuple(codes)
        self.calls.append(batch)
        self.started.set()
        self.release.wait(1.0)
        return ResearchRefreshResult(
            requested_codes=batch,
            completed_codes=batch,
            changed_codes=batch,
            covered_codes=batch,
            data_version="research:changed",
            started_at=observed_at,
            completed_at=observed_at + timedelta(seconds=1),
        )


def _request(strategy: Strategy = Strategy.TOMORROW) -> V2CycleRequest:
    return V2CycleRequest(
        strategy,
        NOW.date(),
        NOW,
        "today_main",
        1,
        f"input:{strategy.value}",
        True,
        NOW.replace(hour=14, minute=48),
    )


def _cadence() -> CadencePolicy:
    return CadencePolicy.from_seconds(
        {
            "full_market": {"today_main": 10},
            "candidate_quotes": {"today_main": 1},
            "topk_quotes": {"today_main": 1},
            "long_quotes": {"today_main": 1},
            "score": {"today_main": 3},
            "industry_heat": {"today_main": 60},
            "market_news": {"today_main": 60},
            "stock_risk": {"today_main": 180},
        }
    )


def test_local_output_is_published_before_non_blocking_research_and_result_requests_rescore() -> None:
    research = _Research()
    results: list[tuple[ResearchRefreshResult, bool]] = []
    runtime = V2ResearchRuntime(
        research,
        cadence=_cadence(),
        now=lambda: NOW,
        on_result=lambda result, initial: results.append((result, initial)),
    )
    runtime.start()
    try:
        intent = V2ResearchIntent(
            Strategy.TOMORROW,
            NOW.date(),
            ("600001",),
            ("600001", "600002", "600003"),
        )

        assert runtime.observe(intent, _request()) is True
        assert research.started.wait(1.0)
        assert research.calls == [("600001",)]
        assert results == []

        research.release.set()
        assert runtime.wait_until_idle(2.0)
    finally:
        research.release.set()
        runtime.stop(wait=True)

    assert [(result.changed_codes, initial) for result, initial in results] == [(("600001",), True)]


def test_periodic_stock_risk_uses_candidates_without_reoffering_on_every_tick() -> None:
    research = _Research()
    research.release.set()
    runtime = V2ResearchRuntime(
        research,
        cadence=_cadence(),
        now=lambda: NOW,
        on_result=lambda _result, _initial: None,
    )
    runtime.start()
    try:
        intent = V2ResearchIntent(
            Strategy.TOMORROW,
            NOW.date(),
            (),
            ("600001", "600002", "600003"),
        )
        assert runtime.observe(intent, _request()) is False
        assert runtime.offer_due(NOW, MarketPhase.TODAY_MAIN, is_trading_day=True) is True
        assert runtime.wait_until_idle(2.0)
        assert (
            runtime.offer_due(
                NOW + timedelta(seconds=30),
                MarketPhase.TODAY_MAIN,
                is_trading_day=True,
            )
            is False
        )
    finally:
        runtime.stop(wait=True)

    assert research.calls == [("600001", "600002", "600003")]


def test_failed_new_output_releases_the_initial_review_barrier() -> None:
    class FailedResearch:
        def refresh_stock_risk(self, codes, observed_at, *, deadline=None):
            del deadline
            batch = tuple(codes)
            return ResearchRefreshResult(
                requested_codes=batch,
                failed_codes=batch,
                data_version="research:failed",
                started_at=observed_at,
                completed_at=observed_at + timedelta(seconds=1),
            )

    results: list[tuple[ResearchRefreshResult, bool]] = []
    runtime = V2ResearchRuntime(
        FailedResearch(),
        cadence=_cadence(),
        now=lambda: NOW,
        on_result=lambda result, initial: results.append((result, initial)),
    )
    runtime.start()
    try:
        intent = V2ResearchIntent(Strategy.TOMORROW, NOW.date(), ("600001",), ("600001",))
        assert runtime.observe(intent, _request()) is True
        assert runtime.wait_until_idle(2.0)
    finally:
        runtime.stop(wait=True)

    assert len(results) == 1
    assert results[0][0].failed_codes == ("600001",)
    assert results[0][1] is True


def test_earlier_batch_does_not_release_later_strategy_initial_barrier() -> None:
    first_started = threading.Event()
    release_first = threading.Event()

    class SequencedResearch:
        def __init__(self) -> None:
            self.calls: list[tuple[str, ...]] = []

        def refresh_stock_risk(self, codes, observed_at, *, deadline=None):
            del deadline
            batch = tuple(codes)
            self.calls.append(batch)
            if len(self.calls) == 1:
                first_started.set()
                release_first.wait(1.0)
            return ResearchRefreshResult(
                requested_codes=batch,
                completed_codes=batch,
                covered_codes=batch,
                data_version=f"research:{len(self.calls)}",
                started_at=observed_at,
                completed_at=observed_at + timedelta(seconds=len(self.calls)),
            )

    research = SequencedResearch()
    results: list[tuple[tuple[str, ...], bool]] = []
    runtime = V2ResearchRuntime(
        research,
        cadence=_cadence(),
        now=lambda: NOW,
        on_result=lambda result, initial: results.append((result.requested_codes, initial)),
    )
    runtime.start()
    try:
        tomorrow = V2ResearchIntent(Strategy.TOMORROW, NOW.date(), ("600001",), ("600001",))
        today = V2ResearchIntent(Strategy.TODAY, NOW.date(), ("600002",), ("600002",))
        assert runtime.observe(tomorrow, _request()) is True
        assert first_started.wait(1.0)
        assert runtime.observe(today, _request(Strategy.TODAY)) is True
        release_first.set()
        assert runtime.wait_until_idle(2.0)
    finally:
        release_first.set()
        runtime.stop(wait=True)

    assert research.calls == [("600001",), ("600002",)]
    assert results == [(("600001",), True), (("600002",), True)]


def test_candidate_already_in_periodic_research_still_defers_first_output_review() -> None:
    research = _Research()
    results: list[tuple[ResearchRefreshResult, bool]] = []
    runtime = V2ResearchRuntime(
        research,
        cadence=_cadence(),
        now=lambda: NOW,
        on_result=lambda result, initial: results.append((result, initial)),
    )
    runtime.start()
    try:
        candidates = V2ResearchIntent(Strategy.TOMORROW, NOW.date(), (), ("600001",))
        assert runtime.observe(candidates, _request()) is False
        assert runtime.offer_due(NOW, MarketPhase.TODAY_MAIN, is_trading_day=True) is True
        assert research.started.wait(1.0)

        promoted = V2ResearchIntent(Strategy.TOMORROW, NOW.date(), ("600001",), ("600001",))
        assert runtime.observe(promoted, _request()) is True
        research.release.set()
        assert runtime.wait_until_idle(2.0)
    finally:
        research.release.set()
        runtime.stop(wait=True)

    assert research.calls == [("600001",)]
    assert [(result.changed_codes, initial) for result, initial in results] == [(("600001",), True)]

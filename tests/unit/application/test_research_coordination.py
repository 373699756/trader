from __future__ import annotations

import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from trader.application.ports.market import ResearchRefreshResult
from trader.application.research_coordination import ResearchCoordinator, ResearchCoordinatorOptions

SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 7, 29, 19, 30, tzinfo=SHANGHAI)


class RecordingResearch:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.completed = threading.Event()

    def refresh_stock_risk(self, codes, observed_at, *, deadline=None):
        batch = tuple(codes)
        self.calls.append(batch)
        if sum(len(item) for item in self.calls) >= 6:
            self.completed.set()
        return ResearchRefreshResult(
            requested_codes=batch,
            completed_codes=batch,
            changed_codes=batch,
            covered_codes=batch,
            data_version="research:test",
            started_at=observed_at,
            completed_at=observed_at + timedelta(seconds=1),
        )


def test_research_coordinator_runs_after_close_in_bounded_priority_batches() -> None:
    research = RecordingResearch()
    results: list[ResearchRefreshResult] = []
    coordinator = ResearchCoordinator(
        research,
        now=lambda: NOW,
        on_result=results.append,
        options=ResearchCoordinatorOptions(
            batch_size=4,
            batch_budget_seconds=40,
            queue_capacity=1,
        ),
    )
    assert coordinator.start() is True
    try:
        coordinator.offer(("600006", "600005", "600004", "600003", "600002", "600001"), NOW)
        assert research.completed.wait(2.0)
    finally:
        coordinator.stop(wait=True)

    assert research.calls == [
        ("600006", "600005", "600004", "600003"),
        ("600002", "600001"),
    ]
    assert [item.completed_codes for item in results] == [
        ("600006", "600005", "600004", "600003"),
        ("600002", "600001"),
    ]
    assert coordinator.status()["state"] == "stopped"


def test_research_coordinator_prioritizes_new_codes_without_cancelling_running_batch() -> None:
    first_started = threading.Event()
    release_first = threading.Event()
    calls: list[tuple[str, ...]] = []

    class BlockingResearch:
        def refresh_stock_risk(self, codes, observed_at, *, deadline=None):
            batch = tuple(codes)
            calls.append(batch)
            if len(calls) == 1:
                first_started.set()
                release_first.wait(2.0)
            return ResearchRefreshResult(
                requested_codes=batch,
                completed_codes=batch,
                changed_codes=batch,
                covered_codes=batch,
                data_version=f"research:{len(calls)}",
                started_at=observed_at,
                completed_at=observed_at,
            )

    coordinator = ResearchCoordinator(
        BlockingResearch(),
        now=lambda: NOW,
        on_result=lambda _result: None,
        options=ResearchCoordinatorOptions(
            batch_size=2,
            batch_budget_seconds=40,
            queue_capacity=1,
        ),
    )
    coordinator.start()
    try:
        coordinator.offer(("600001", "600002", "600003"), NOW)
        assert first_started.wait(1.0)
        coordinator.offer(("600009", "600003"), NOW)
        release_first.set()
        assert coordinator.wait_until_idle(2.0)
    finally:
        release_first.set()
        coordinator.stop(wait=True)

    assert calls[0] == ("600001", "600002")
    assert calls[1] == ("600009", "600003")

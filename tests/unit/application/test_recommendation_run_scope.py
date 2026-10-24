from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from trader.recommendation.application.ports.runtime import TradingCalendarUnavailableError
from trader.recommendation.application.runtime.run_scope import RecommendationRunScope

SHANGHAI = ZoneInfo("Asia/Shanghai")
FRIDAY = date(2026, 10, 23)
MONDAY = date(2026, 10, 26)
SATURDAY = datetime(2026, 10, 24, 10, 0, tzinfo=SHANGHAI)


def test_preview_scope_resolves_once_and_uses_only_ephemeral_sequences() -> None:
    calls: list[str] = []
    clock = [SATURDAY]
    scope = RecommendationRunScope(
        True, lambda: calls.append("prepare"), lambda: (FRIDAY, MONDAY), lambda: clock[0], lambda: "run-a"
    )
    with ThreadPoolExecutor(max_workers=4) as pool:
        tuple(pool.map(lambda _: scope.initialize(), range(8)))
    assert scope.resolved and scope.is_preview
    preview = scope.preview
    assert preview is not None and preview.market_trade_date == FRIDAY and preview.started_at == SATURDAY
    assert calls == ["prepare"]
    clock[0] = SATURDAY + timedelta(days=2)
    scope.initialize()
    scope.run_live(lambda: calls.append("write"))
    assert scope.preview is preview and calls == ["prepare"]
    assert [scope.next_sequence(lambda: pytest.fail("durable allocator called")) for _ in range(3)] == [1, 3, 5]


def test_live_scope_delegates_sequences_and_remains_live_across_midnight() -> None:
    calls: list[str] = []
    clock = [SATURDAY - timedelta(days=1)]
    scope = RecommendationRunScope(
        True,
        lambda: calls.append("prepare"),
        lambda: (FRIDAY, MONDAY),
        lambda: clock[0],
        lambda: pytest.fail("live run id"),
    )
    scope.initialize()
    assert scope.resolved and not scope.is_preview and scope.preview is None
    scope.run_live(lambda: calls.append("write"))
    assert scope.next_sequence(lambda: 71) == 71
    clock[0] = SATURDAY
    scope.initialize()
    assert not scope.is_preview and calls == ["prepare", "write"]


@pytest.mark.parametrize(
    "failure", (OSError("reference unavailable"), TradingCalendarUnavailableError("calendar unavailable"))
)
def test_unavailable_references_keep_scope_unresolved_until_retry(failure: Exception) -> None:
    ready = [False]
    writes: list[str] = []

    def prepare() -> None:
        if not ready[0]:
            raise failure

    scope = RecommendationRunScope(True, prepare, lambda: (FRIDAY, MONDAY), lambda: SATURDAY, lambda: "retry-a")
    scope.initialize()
    assert not scope.resolved and not scope.is_preview and scope.preview is None
    scope.run_live(lambda: writes.append("write"))
    assert not writes
    with pytest.raises(RuntimeError, match="unresolved"):
        scope.next_sequence(lambda: 1)
    ready[0] = True
    scope.initialize()
    assert scope.resolved and scope.is_preview


@pytest.mark.parametrize("calendar", ((), (MONDAY, FRIDAY), (FRIDAY, FRIDAY), (MONDAY,)))
def test_untrusted_or_missing_prior_calendar_session_remains_unresolved(calendar: tuple[date, ...]) -> None:
    scope = RecommendationRunScope(True, lambda: None, lambda: calendar, lambda: SATURDAY, lambda: "run-a")
    scope.initialize()
    assert not scope.resolved


def test_disabled_scope_does_not_prepare_or_consult_calendar() -> None:
    scope = RecommendationRunScope(
        False,
        lambda: pytest.fail("prepare"),
        lambda: pytest.fail("calendar"),
        lambda: pytest.fail("clock"),
        lambda: pytest.fail("id"),
    )
    scope.initialize()
    assert scope.resolved and not scope.is_preview
    assert scope.next_sequence(lambda: 9) == 9


def test_unresolved_scope_replays_deferred_live_initializers_in_order_once() -> None:
    ready = [False]
    calls: list[str] = []

    def calendar() -> tuple[date, ...]:
        if not ready[0]:
            raise TradingCalendarUnavailableError("calendar_unavailable")
        return FRIDAY, MONDAY

    scope = RecommendationRunScope(True, lambda: None, calendar, lambda: SATURDAY - timedelta(days=1), lambda: "unused")
    scope.initialize()

    def initialize(name: str) -> None:
        assert not scope.resolved
        with pytest.raises(RuntimeError, match="unresolved"):
            scope.next_sequence(lambda: pytest.fail("partially initialized durable sequence"))
        calls.append(name)

    scope.run_live(lambda: initialize("sequence"))
    scope.run_live(lambda: initialize("records"))
    scope.run_live(lambda: initialize("trace"))
    scope.run_live(lambda: initialize("outcomes"))
    scope.run_live(lambda: initialize("data_plane"))
    scope.run_live(lambda: initialize("restore"))
    assert not calls and not scope.resolved
    ready[0] = True
    scope.initialize()
    assert calls == ["sequence", "records", "trace", "outcomes", "data_plane", "restore"]
    assert scope.resolved and not scope.is_preview
    assert scope.next_sequence(lambda: 47) == 47
    scope.initialize()
    assert len(calls) == 6


def test_unresolved_scope_discards_deferred_formal_initializers_when_preview_is_selected() -> None:
    ready = [False]
    calls: list[str] = []

    def prepare() -> None:
        if not ready[0]:
            raise OSError("reference_unavailable")

    scope = RecommendationRunScope(
        True, prepare, lambda: (FRIDAY, MONDAY), lambda: SATURDAY, lambda: "preview-recovery"
    )
    scope.initialize()
    scope.run_live(lambda: calls.append("records"))
    scope.run_live(lambda: calls.append("restore"))
    ready[0] = True
    scope.initialize()
    assert scope.resolved and scope.is_preview and not calls
    scope.initialize()
    scope.run_live(lambda: calls.append("late_write"))
    assert not calls
    assert scope.next_sequence(lambda: pytest.fail("preview durable allocator")) == 1


def test_failed_deferred_initializer_keeps_only_unfinished_work_and_live_mode_for_retry() -> None:
    ready = [False]
    storage_ready = [False]
    clock = [SATURDAY - timedelta(days=1)]
    calls: list[str] = []
    attempts: list[str] = []

    def prepare() -> None:
        if not ready[0]:
            raise OSError("reference_unavailable")

    scope = RecommendationRunScope(True, prepare, lambda: (FRIDAY, MONDAY), lambda: clock[0], lambda: "unused")
    scope.initialize()
    scope.run_live(lambda: calls.append("sequence"))

    def records() -> None:
        attempts.append("records")
        if not storage_ready[0]:
            raise OSError("records_unavailable")
        calls.append("records")

    scope.run_live(records)
    scope.run_live(lambda: calls.append("restore"))
    ready[0] = True
    scope.initialize()
    assert calls == ["sequence"] and attempts == ["records"]
    assert not scope.resolved and not scope.is_preview
    scope.run_live(lambda: calls.append("late_initializer"))
    storage_ready[0] = True
    clock[0] = SATURDAY
    scope.initialize()
    assert calls == ["sequence", "records", "restore", "late_initializer"]
    assert attempts == ["records", "records"]
    assert scope.resolved and not scope.is_preview
    scope.initialize()
    assert attempts == ["records", "records"]

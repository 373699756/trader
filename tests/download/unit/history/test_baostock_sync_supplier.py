from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from typing import cast

import pytest

from tests.helpers.download.history.baostock_gateway import _Result
from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.domain.history_sync import HistorySyncProgress
from tests.helpers.download.configuration import HistorySyncConfigurationFixture as HistorySyncConfiguration
from trader.download.infra.baostock_gateway import BaoStockRowResult
from trader.download.infra.baostock_sync_supplier import (
    BaoStockHistorySupplier,
    BaoStockRequestPolicy,
    _Activity,
    _Attempt,
    _RateLimitedSdk,
    _RequestState,
)


@dataclass
class _ProgressRecorder:
    values: list[HistorySyncProgress] = field(default_factory=list)

    def publish(self, progress: HistorySyncProgress) -> None:
        self.values.append(progress)


@dataclass
class _Clock:
    value: float = 0.0
    sleeps: list[float] = field(default_factory=list)

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


def test_supplier_serializes_callers_and_rejects_another_vendor_owner(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from time import sleep
    from trader.infra.process_lock import ProcessLock, ProcessLockError

    supplier = BaoStockHistorySupplier(HistorySyncConfiguration(tmp_path / "history", tmp_path / "reference"))
    running = 0
    maximum = 0
    barrier = Barrier(2)

    def request(_command):
        nonlocal running, maximum
        running += 1
        maximum = max(maximum, running)
        sleep(0.02)
        running -= 1
        return None

    monkeypatch.setattr(supplier, "_serial_request", request)

    def caller():
        barrier.wait(timeout=2)
        supplier._request(None)

    with ThreadPoolExecutor(max_workers=2) as workers:
        list(workers.map(lambda _: caller(), range(2)))
    assert maximum == 1
    path = tmp_path / "vendor.lock"
    with ProcessLock(path):
        with BaoStockHistorySupplier(HistorySyncConfiguration(tmp_path / "history", tmp_path / "reference"), vendor_lock=ProcessLock(path)) as other:
            with pytest.raises(ProcessLockError):
                other._start(_Attempt(1, 1))


def test_price_attempt_deadline_survives_pages_but_resets_for_each_retry(tmp_path, monkeypatch) -> None:
    import trader.download.infra.baostock_sync_supplier as supplier_module

    clock = _Clock()
    supplier = BaoStockHistorySupplier(
        HistorySyncConfiguration(history_root=tmp_path / "history", reference_root=tmp_path / "reference", supplier_retries=3),
        monotonic=clock,
        sleep=clock.sleep,
        price_total_seconds=45.0,
    )
    attempts = []

    def request(command, state, attempt, timeout):
        attempts.append((attempt.number, clock.value, supplier._request_deadline))
        clock.value += 45.0
        failure = RuntimeError(f"fixture_attempt_{attempt.number}")
        return supplier_module._AttemptResult(None, state, "supplier_price_total_timeout", failure)

    monkeypatch.setattr(supplier, "_request_once", request)
    monkeypatch.setattr(supplier, "close", lambda: None)
    security = BaoStockSecurity("600001", "fixture", "main", date(2000, 1, 1), None, "fixture")
    with pytest.raises(RuntimeError, match="supplier_price_total_timeout") as caught:
        supplier.fetch_code(security, (date(2026, 10, 9),))
    assert [number for number, _, _ in attempts] == [1, 2, 3, 4]
    assert [started for _, started, _ in attempts] == pytest.approx([0.0, 60.0, 135.0, 240.0])
    assert [deadline - started for _, started, deadline in attempts] == pytest.approx([45.0] * 4)
    assert clock.value == pytest.approx(4 * 45.0 + 15.0 + 30.0 + 60.0)
    assert sum(clock.sleeps) == pytest.approx(105.0)
    assert str(caught.value.__cause__) == "fixture_attempt_4"
    clock.value = 44.0
    supplier._request_deadline = 45.0
    state = supplier._handle_activity(
        _Activity("supplier_daily_raw", "started"), _RequestState("supplier_daily_raw", None, 0), _Attempt(1, 4), 45
    )
    clock.value = 45.0
    with pytest.raises(RuntimeError, match="supplier_price_total_timeout"):
        supplier._receive(None, state, _Attempt(1, 4))


def test_worker_reports_price_fact_mismatch_without_hiding_it_as_supplier_failure():
    from trader.download.infra.baostock_sync_supplier import _failure_code

    assert (
        _failure_code(ValueError("BaoStock code download facts do not match its daily batch"))
        == "supplier_daily_facts_mismatch"
    )
    assert _failure_code(ValueError("unexpected private payload")) == "supplier_failed"


class _SilentConnection:
    def __init__(self, clock: _Clock) -> None:
        self._clock = clock
        self.sent: list[object] = []

    def send(self, value: object) -> None:
        self.sent.append(value)

    def poll(self, timeout: float) -> bool:
        self._clock.value += timeout
        return False

    def close(self) -> None:
        pass


class _TimedConnection(_SilentConnection):
    def __init__(self, clock: _Clock) -> None:
        super().__init__(clock)
        self._events: list[tuple[float, object]] = [
            (0.0, _Activity("supplier_calendar", "started")),
            (0.75, _Activity("supplier_calendar", "returned")),
        ]

    def poll(self, timeout: float) -> bool:
        if self._events and self._clock.value + timeout >= self._events[0][0]:
            self._clock.value = self._events[0][0]
            return True
        self._clock.value += timeout
        return False

    def recv(self) -> object:
        return self._events.pop(0)[1]


class _LiveProcess:
    def __init__(self) -> None:
        self.alive = True

    def is_alive(self) -> bool:
        return self.alive

    def join(self, timeout: float | None = None) -> None:
        del timeout
        self.alive = False

    def terminate(self) -> None:
        self.alive = False


def test_price_poll_deadline_stops_pages_and_allows_next_stock(tmp_path, monkeypatch) -> None:
    from multiprocessing.connection import Connection
    from multiprocessing.process import BaseProcess
    from trader.download.infra import baostock_sync_supplier as supplier_module
    from trader.infra.process_lock import ProcessLock
    from tests.helpers.download.history.infra.history_sync import _download

    clock = _Clock()
    starts = []
    processes = []
    days = (date(2026, 10, 9),)
    security = BaoStockSecurity("600001", "fixture", "main", date(2000, 1, 1), None, "fixture")
    next_security = BaoStockSecurity("600002", "fixture", "main", date(2000, 1, 1), None, "fixture")
    vendor_lock = ProcessLock(tmp_path / "vendor.lock")
    supplier = BaoStockHistorySupplier(
        HistorySyncConfiguration(history_root=tmp_path / "history", reference_root=tmp_path / "reference", supplier_retries=3),
        monotonic=clock,
        sleep=clock.sleep,
        price_total_seconds=45.0,
        vendor_lock=vendor_lock,
    )

    def start(attempt):
        starts.append(attempt.number)
        vendor_lock.acquire()
        connection = _TimedConnection(clock)
        if len(starts) <= 4:
            started_at = clock.value
            connection._events = [
                (started_at, _Activity("supplier_daily_raw", "started")),
                (started_at + 14.0, _Activity("supplier_daily_raw", "returned")),
                (started_at + 15.0, _Activity("supplier_daily_raw", "started")),
                (started_at + 29.0, _Activity("supplier_daily_raw", "returned")),
                (started_at + 30.0, _Activity("supplier_daily_qfq", "started")),
                (started_at + 44.0, _Activity("supplier_daily_qfq", "returned")),
                (started_at + 44.5, _Activity("supplier_daily_qfq", "started")),
            ]
        else:
            connection._events = [
                (clock.value + 1.0, supplier_module._Response(download=_download(next_security.code, days, 1))),
            ]
        process = _LiveProcess()
        processes.append(process)
        supplier._process = cast(BaseProcess, process)
        supplier._connection = cast(Connection, connection)

    monkeypatch.setattr(supplier, "_start", start)
    with pytest.raises(RuntimeError, match="supplier_price_total_timeout"):
        supplier.fetch_code(security, days)
    assert clock.value == pytest.approx(285.0)
    assert starts == [1, 2, 3, 4]
    assert all(not process.alive for process in processes)
    assert supplier.fetch_code(next_security, days).batch.cells
    assert starts == [1, 2, 3, 4, 1]
    assert clock.value == pytest.approx(286.0)
    supplier.close()
    with ProcessLock(tmp_path / "vendor.lock"):
        pass


class _Sdk:
    __version__ = "test"

    def query_history_k_data_plus(self, *_args: object, **_kwargs: object) -> BaoStockRowResult:
        return cast(BaoStockRowResult, object())


def test_worker_activity_identifies_raw_and_qfq_supplier_calls() -> None:
    values: list[tuple[str, str, str | None]] = []
    sdk = _RateLimitedSdk(
        _Sdk(),  # type: ignore[arg-type]  # focused SDK boundary double
        lambda stage, state, current: values.append((stage, state, current)),
        2.0,
        monotonic=lambda: 0.0,
        sleep=lambda _seconds: None,
    )

    sdk.query_history_k_data_plus(
        "sh.600001",
        "date,code",
        "2020-01-01",
        "2026-09-11",
        frequency="d",
        adjustflag="3",
    )
    sdk.query_history_k_data_plus(
        "sh.600001",
        "date,code",
        "2020-01-01",
        "2026-09-11",
        frequency="d",
        adjustflag="2",
    )

    assert values == [
        ("supplier_daily_raw", "started", "sh.600001"),
        ("supplier_daily_raw", "returned", "sh.600001"),
        ("supplier_daily_qfq", "started", "sh.600001"),
        ("supplier_daily_qfq", "returned", "sh.600001"),
    ]


def test_preparation_pages_and_daily_calls_share_the_session_rate_timeline() -> None:
    clock = _Clock()
    starts: list[tuple[str, float]] = []
    activity: list[tuple[str, str, str | None]] = []

    class Pages:
        error_code = "0"
        error_msg = ""
        fields = ("code",)

        def __init__(self) -> None:
            self.consumed = 0

        def next(self) -> bool:
            if self.consumed in (2000, 4000):
                starts.append(("page", clock.value))
            return self.consumed < 4001

        def get_row_data(self):
            self.consumed += 1
            return ("sh.600001",)

    class Sdk(_Sdk):
        def query_stock_basic(self):
            starts.append(("universe", clock.value))
            return Pages()

        def query_history_k_data_plus(self, *_args, **_kwargs):
            starts.append(("daily", clock.value))
            return Pages()

    def sleep(seconds: float) -> None:
        clock.value += seconds

    sdk = _RateLimitedSdk(Sdk(), lambda *value: activity.append(value), 1.5, monotonic=clock, sleep=sleep)
    result = sdk.query_stock_basic()
    while result.next():
        result.get_row_data()
    assert starts == [("universe", 2), ("page", 4), ("page", 6)]
    for flag in ("3", "2"):
        daily = sdk.query_history_k_data_plus(
            "sh.600001", "date", "2026-01-01", "2026-10-09", frequency="d", adjustflag=flag
        )
        if flag == "3":
            while daily.next():
                daily.get_row_data()
    assert starts[-4:] == [("daily", 8), ("page", 9.5), ("page", 11), ("daily", 12.5)]
    assert len([value for value in activity if value[:2] == ("supplier_universe", "started")]) == 3
    restarted = _RateLimitedSdk(Sdk(), lambda *_: None, 1.5, monotonic=clock, sleep=sleep)
    restarted.query_stock_basic()
    assert starts[-1] == ("universe", 14.5)


def test_supplier_reports_waiting_heartbeats_and_the_timed_out_stage(tmp_path) -> None:
    recorder = _ProgressRecorder()
    clock = _Clock()
    supplier = BaoStockHistorySupplier(
        HistorySyncConfiguration(
            history_root=tmp_path / "history",
            reference_root=tmp_path / "reference",
            supplier_timeout_seconds=1.0,
            supplier_retries=0,
            progress_heartbeat_seconds=0.25,
        ),
        progress=recorder,
        monotonic=clock,
    )
    supplier._process = _LiveProcess()  # type: ignore[assignment]  # bounded process test double
    supplier._connection = _SilentConnection(clock)  # type: ignore[assignment]  # bounded IPC test double

    with pytest.raises(RuntimeError, match="supplier_calendar_timeout"):
        supplier.load_context(date(2026, 9, 11), 2000, universe=())

    waiting = [item for item in recorder.values if item.state == "waiting"]
    assert waiting
    assert all(item.stage == "supplier_calendar" for item in waiting)
    assert waiting[-1].call_elapsed_seconds >= 0.75
    assert recorder.values[-1].state == "failed"
    assert recorder.values[-1].stage == "supplier_calendar"
    assert recorder.values[-1].failure_reason == "supplier_calendar_timeout"
    assert all(item.data_source == "BaoStock" for item in recorder.values)


def test_supplier_result_handle_does_not_restart_the_call_deadline(tmp_path) -> None:
    recorder = _ProgressRecorder()
    clock = _Clock()
    supplier = BaoStockHistorySupplier(
        HistorySyncConfiguration(
            history_root=tmp_path / "history",
            reference_root=tmp_path / "reference",
            supplier_timeout_seconds=1.0,
            supplier_retries=0,
            progress_heartbeat_seconds=0.25,
        ),
        progress=recorder,
        monotonic=clock,
    )
    supplier._process = _LiveProcess()  # type: ignore[assignment]  # bounded process test double
    supplier._connection = _TimedConnection(clock)  # type: ignore[assignment]  # bounded IPC test double

    with pytest.raises(RuntimeError, match="supplier_calendar_timeout"):
        supplier.load_context(date(2026, 9, 11), 2000, universe=())

    assert recorder.values[-1].state == "failed"
    assert recorder.values[-1].call_elapsed_seconds == pytest.approx(1.0)
    assert all(item.data_source == "BaoStock" for item in recorder.values)


def test_supplier_keeps_context_and_price_request_policies_separate(tmp_path, monkeypatch) -> None:
    from trader.download.infra import baostock_sync_supplier as supplier_module

    supplier = BaoStockHistorySupplier(
        HistorySyncConfiguration(
            history_root=tmp_path / "history",
            reference_root=tmp_path / "reference",
            supplier_timeout_seconds=45.0,
            supplier_retries=2,
            progress_heartbeat_seconds=5.0,
        ),
        price_policy=BaoStockRequestPolicy(20.0, 0),
    )
    calls: list[tuple[str, int, int, float]] = []

    def fail(command, state, attempt, timeout_seconds):
        calls.append((type(command).__name__, attempt.number, attempt.total, timeout_seconds))
        return supplier_module._AttemptResult(None, state, "supplier_failed")

    monkeypatch.setattr(supplier, "_request_once", fail)
    with pytest.raises(RuntimeError, match="supplier_failed"):
        supplier.load_context(date(2026, 10, 9), 2000, universe=())
    security = BaoStockSecurity("600001", "fixture", "main", date(2000, 1, 1), None, "fixture")
    with pytest.raises(RuntimeError, match="supplier_failed"):
        supplier.fetch_code(security, (date(2026, 10, 9),))

    assert calls == [
        ("_LoadContext", 1, 3, 45.0),
        ("_LoadContext", 2, 3, 45.0),
        ("_LoadContext", 3, 3, 45.0),
        ("_FetchCode", 1, 1, 20.0),
    ]


@pytest.mark.parametrize("recovery_attempt", (2, 3, 4, None))
@pytest.mark.parametrize("failure_reason", ("supplier_daily_raw_timeout", "supplier_login_timeout"))
def test_price_timeout_restarts_session_with_bounded_retries(tmp_path, monkeypatch, recovery_attempt, failure_reason):
    from trader.download.infra import baostock_sync_supplier as supplier_module
    from tests.helpers.download.history.infra.history_sync import _download

    failures = []
    clock = _Clock()
    recorder = _ProgressRecorder()
    supplier = BaoStockHistorySupplier(HistorySyncConfiguration(
        history_root=tmp_path / "history", reference_root=tmp_path / "reference", supplier_retries=3
    ), failures=lambda context, exc: failures.append((context, str(exc))), monotonic=clock, sleep=clock.sleep,
       price_total_seconds=45.0, progress=recorder)
    attempts, starts, closes = [], [], []
    security = BaoStockSecurity("600001", "fixture", "main", date(2000, 1, 1), None, "fixture")
    days = (date(2026, 10, 9),)

    def request(command, state, attempt, timeout):
        attempts.append(attempt.number)
        starts.append(clock.value)
        clock.value += 2.0
        if attempt.number == recovery_attempt:
            return supplier_module._AttemptResult(
                supplier_module._Response(download=_download(security.code, days, 1)), state, None
            )
        if failure_reason == "supplier_login_timeout":
            state = replace(state, stage="supplier_login", current_item=None)
        return supplier_module._AttemptResult(None, state, failure_reason)

    monkeypatch.setattr(supplier, "_request_once", request)
    monkeypatch.setattr(supplier, "close", lambda: closes.append(True))
    if recovery_attempt is not None:
        assert supplier.fetch_code(security, days).batch.cells
    else:
        with pytest.raises(RuntimeError, match=failure_reason):
            supplier.fetch_code(security, days)
    attempted = recovery_attempt or 4
    failed = attempted - 1 if recovery_attempt is not None else 4
    assert attempts == list(range(1, attempted + 1)) and len(closes) == failed
    assert [later - earlier - 2.0 for earlier, later in zip(starts[:-1], starts[1:], strict=True)] == pytest.approx(
        [15.0, 30.0, 60.0][:attempted - 1]
    )
    assert sum(clock.sleeps) == pytest.approx(sum([15.0, 30.0, 60.0][:attempted - 1]))
    assert all(0 < seconds <= 0.1 for seconds in clock.sleeps)
    assert [context.attempt for context, _ in failures] == list(range(1, failed + 1))
    assert len(failures) == failed
    assert failures[0][0].code == security.code and failures[0][0].supplier == "baostock"
    assert all(context.maximum_attempts == 4 for context, _ in failures)
    assert failures[0][0].window_start == failures[0][0].window_end == days[0]
    assert failures[0][1] == failure_reason
    retries = [item for item in recorder.values if item.state == "retrying"]
    assert [item.attempt for item in retries] == list(range(2, attempted + 1))
    assert all(item.max_attempts == 4 and item.data_source == "BaoStock" for item in retries)
    assert [item.retry_delay_seconds for item in retries] == [15.0, 30.0, 60.0][:attempted - 1]


def test_late_success_is_discarded_before_retrying_with_a_fresh_price_deadline(tmp_path, monkeypatch):
    from trader.download.infra import baostock_sync_supplier as supplier_module
    from tests.helpers.download.history.infra.history_sync import _download

    clock = _Clock()
    attempts, failures, closes = [], [], []
    security = BaoStockSecurity("600001", "fixture", "main", date(2000, 1, 1), None, "fixture")
    days = (date(2026, 10, 9),)
    late, valid = _download(security.code, days, 1), _download(security.code, days, 2)
    supplier = BaoStockHistorySupplier(
        HistorySyncConfiguration(tmp_path / "history", tmp_path / "reference", supplier_retries=3),
        monotonic=clock, sleep=clock.sleep, price_total_seconds=45.0,
        failures=lambda context, exc: failures.append((context, str(exc))),
    )

    def request(command, state, attempt, timeout):
        attempts.append((attempt.number, clock.value, supplier._request_deadline))
        clock.value += 46.0 if attempt.number == 1 else 1.0
        return supplier_module._AttemptResult(
            supplier_module._Response(download=late if attempt.number == 1 else valid), state, None,
        )

    monkeypatch.setattr(supplier, "_request_once", request)
    monkeypatch.setattr(supplier, "close", lambda: closes.append(True))
    assert supplier.fetch_code(security, days) is valid
    assert [number for number, _, _ in attempts] == [1, 2]
    assert [started for _, started, _ in attempts] == pytest.approx([0.0, 61.0])
    assert [deadline - started for _, started, deadline in attempts] == pytest.approx([45.0, 45.0])
    assert clock.value == pytest.approx(62.0) and sum(clock.sleeps) == pytest.approx(15.0)
    assert len(closes) == len(failures) == 1
    assert failures[0][0].attempt == 1 and failures[0][0].maximum_attempts == 4
    assert failures[0][1] == "supplier_price_total_timeout"


def test_price_retry_backoff_is_cancelled_without_starting_another_attempt(tmp_path, monkeypatch):
    from trader.download.infra import baostock_sync_supplier as supplier_module

    clock = _Clock()
    attempts, failures = [], []
    supplier = BaoStockHistorySupplier(
        HistorySyncConfiguration(tmp_path / "history", tmp_path / "reference", supplier_retries=3),
        monotonic=clock, sleep=clock.sleep, cancel_requested=lambda: clock.value >= 0.2,
        price_total_seconds=45.0, failures=lambda context, exc: failures.append((context, str(exc))),
    )

    def fail(command, state, attempt, timeout):
        attempts.append(attempt.number)
        return supplier_module._AttemptResult(None, state, "supplier_daily_raw_timeout")

    monkeypatch.setattr(supplier, "_request_once", fail)
    monkeypatch.setattr(supplier, "close", lambda: None)
    security = BaoStockSecurity("600001", "fixture", "main", date(2000, 1, 1), None, "fixture")
    with pytest.raises(RuntimeError, match="supplier_cancelled"):
        supplier.fetch_code(security, (date(2026, 10, 9),))
    assert attempts == [1] and clock.value == pytest.approx(0.2)
    assert sum(clock.sleeps) == pytest.approx(0.2) and max(clock.sleeps) <= 0.1
    assert len(failures) == 1 and failures[0][1] == "supplier_daily_raw_timeout"


@pytest.mark.parametrize(("deadline", "expected_wait"), ((float("inf"), 10.0), (45.0, 3.0), (40.0, 0.0)))
def test_close_bounds_grace_by_remaining_price_deadline(tmp_path, deadline, expected_wait):
    from multiprocessing.connection import Connection
    from multiprocessing.process import BaseProcess
    from trader.infra.process_lock import ProcessLock

    clock = _Clock(42.0)
    waits = []

    class BlockedProcess(_LiveProcess):
        def join(self, timeout=None):
            waits.append(timeout)

    process = BlockedProcess()
    lock_path = tmp_path / "vendor.lock"
    vendor_lock = ProcessLock(lock_path)
    vendor_lock.acquire()
    supplier = BaoStockHistorySupplier(
        HistorySyncConfiguration(history_root=tmp_path / "history", reference_root=tmp_path / "reference"),
        monotonic=clock,
        vendor_lock=vendor_lock,
    )
    supplier._request_deadline = deadline
    supplier._process = cast(BaseProcess, process)
    supplier._connection = cast(Connection, _SilentConnection(clock))
    supplier.close()
    assert waits == [expected_wait, 1.0]
    assert not process.alive
    assert supplier._process is None and supplier._connection is None
    with ProcessLock(lock_path):
        pass


def test_supplier_login_timeout_does_not_repeat_the_slow_start(tmp_path, monkeypatch) -> None:
    from trader.download.infra import baostock_sync_supplier as supplier_module

    clock = _Clock()
    supplier = BaoStockHistorySupplier(
        HistorySyncConfiguration(
            history_root=tmp_path / "history",
            reference_root=tmp_path / "reference",
            supplier_timeout_seconds=45.0,
            supplier_retries=3,
        ),
        monotonic=clock,
        sleep=clock.sleep,
    )
    attempts = []

    def fail(_command, state, attempt, _timeout_seconds):
        attempts.append(attempt.number)
        return supplier_module._AttemptResult(None, state, "supplier_login_timeout")

    monkeypatch.setattr(supplier, "_request_once", fail)
    with pytest.raises(RuntimeError, match="supplier_login_timeout"):
        supplier.load_context(date(2026, 10, 9), 2000, universe=())

    assert attempts == [1]
    assert clock.sleeps == []


@pytest.mark.parametrize(("restart_session", "expected_closes"), ((False, 0), (True, 1)))
def test_price_failure_restarts_only_an_unhealthy_supplier_session(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    restart_session: bool,
    expected_closes: int,
) -> None:
    from trader.download.infra import baostock_sync_supplier as supplier_module

    clock = _Clock()
    supplier = BaoStockHistorySupplier(
        HistorySyncConfiguration(
            history_root=tmp_path / "history", reference_root=tmp_path / "reference", supplier_retries=0
        ),
        price_policy=BaoStockRequestPolicy(20.0, 0),
        monotonic=clock,
        sleep=clock.sleep,
    )
    closes = 0

    def fail(_command, state, _attempt, _timeout_seconds):
        from trader.download.infra.history_sync_failure import BuildHistoryFailureEvidence

        reason = "supplier_query_session_failed" if restart_session else "qfq_daily_query_failed"
        response = supplier_module._Response(
            failure_reason=reason,
            restart_session=restart_session,
            failure_evidence=BuildHistoryFailureEvidence(RuntimeError(reason)),
        )
        return supplier_module._AttemptResult(response, state, response.failure_reason)

    def close() -> None:
        nonlocal closes
        closes += 1

    monkeypatch.setattr(supplier, "_request_once", fail)
    monkeypatch.setattr(supplier, "close", close)
    security = BaoStockSecurity("600001", "fixture", "main", date(2000, 1, 1), None, "fixture")

    with pytest.raises(RuntimeError, match="query"):
        supplier.fetch_code(security, (date(2026, 10, 9),))

    assert closes == expected_closes
    assert clock.sleeps == []


@pytest.mark.parametrize(("timeout_seconds", "retries"), ((float("nan"), 0), (0.0, 0), (20.0, -1), (20.0, 4)))
def test_supplier_request_policy_rejects_unbounded_values(timeout_seconds: float, retries: int) -> None:
    with pytest.raises(ValueError, match="request policy"):
        BaoStockRequestPolicy(timeout_seconds, retries)


def test_supplier_request_policy_accepts_three_retries() -> None:
    assert BaoStockRequestPolicy(45.0, 3).retries == 3


def test_worker_context_keeps_official_universe_without_querying_baostock_industry(monkeypatch) -> None:
    from trader.download.infra import baostock_sync_supplier as supplier_module

    dates = (date(2026, 10, 8), date(2026, 10, 9))
    universe = (BaoStockSecurity("302132", "fixture", "chinext", dates[0], None, "exchange_security_master"),)
    calls: list[str] = []

    class Sdk(_Sdk):
        def login(self):
            calls.append("login")
            return _Result((), ())

        def logout(self):
            calls.append("logout")
            return _Result((), ())

        def query_stock_basic(self):
            raise AssertionError("history must not reload BaoStock's universe")

        def query_trade_dates(self, **_kwargs):
            calls.append("calendar")
            return _Result(("calendar_date", "is_trading_day"), tuple((day.isoformat(), "1") for day in dates))

        def query_stock_industry(self, **_kwargs):
            raise AssertionError("BaoStock industry queries have been removed")

    class Connection:
        def __init__(self):
            self.commands = iter(
                (
                    supplier_module._LoadContext(dates[-1], 2, universe),
                    supplier_module._Stop(),
                )
            )
            self.responses = []
            self.closed = False

        def recv(self):
            return next(self.commands)

        def send(self, value):
            self.responses.append(value)

        def close(self):
            self.closed = True

    connection = Connection()
    rate_limited_sdk = supplier_module._RateLimitedSdk
    monkeypatch.setattr(supplier_module, "_silence_vendor_output", lambda: None)
    monkeypatch.setattr(supplier_module, "LoadBaostockSdk", Sdk)
    monkeypatch.setattr(supplier_module, "BaostockDependencyVersions", lambda: ())
    monkeypatch.setattr(
        supplier_module,
        "_RateLimitedSdk",
        lambda sdk, activity, interval: rate_limited_sdk(
            sdk, activity, interval, monotonic=lambda: 0.0, sleep=lambda _seconds: None
        ),
    )
    supplier_module._worker_main(connection, 1.5)
    context = next(item.context for item in connection.responses if isinstance(item, supplier_module._Response))
    assert context is not None
    assert context.calendar.open_dates == dates
    assert context.universe == universe
    assert context.industry_intervals == ()
    assert calls == ["login", "calendar", "logout"]
    assert not any(isinstance(item, HistorySyncProgress) for item in connection.responses)
    assert connection.closed

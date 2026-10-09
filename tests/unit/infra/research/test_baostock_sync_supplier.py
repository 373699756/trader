from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import cast

import pytest

from trader.download.domain.history_sync import HistorySyncConfiguration, HistorySyncProgress
from trader.download.infra.baostock_gateway import BaoStockRowResult
from trader.download.infra.baostock_sync_supplier import BaoStockHistorySupplier, _Activity, _RateLimitedSdk


@dataclass
class _ProgressRecorder:
    values: list[HistorySyncProgress] = field(default_factory=list)

    def publish(self, progress: HistorySyncProgress) -> None:
        self.values.append(progress)


@dataclass
class _Clock:
    value: float = 0.0

    def __call__(self) -> float:
        return self.value


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
        self._events = [
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


def test_supplier_reports_waiting_heartbeats_and_the_timed_out_stage() -> None:
    recorder = _ProgressRecorder()
    clock = _Clock()
    supplier = BaoStockHistorySupplier(
        HistorySyncConfiguration(
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
        supplier.load_context(date(2026, 9, 11), 2000)

    waiting = [item for item in recorder.values if item.state == "waiting"]
    assert waiting
    assert all(item.stage == "supplier_calendar" for item in waiting)
    assert waiting[-1].call_elapsed_seconds >= 0.75
    assert recorder.values[-1].state == "failed"
    assert recorder.values[-1].stage == "supplier_calendar"


def test_supplier_result_handle_does_not_restart_the_call_deadline() -> None:
    recorder = _ProgressRecorder()
    clock = _Clock()
    supplier = BaoStockHistorySupplier(
        HistorySyncConfiguration(
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
        supplier.load_context(date(2026, 9, 11), 2000)

    assert recorder.values[-1].state == "failed"
    assert recorder.values[-1].call_elapsed_seconds == pytest.approx(1.0)

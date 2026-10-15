from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import cast

import pytest

from tests.unit.infra.research.test_baostock_gateway import _Result
from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.domain.history_sync import HistorySyncConfiguration, HistorySyncProgress
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

    def __call__(self) -> float:
        return self.value


def test_price_total_deadline_survives_page_activity_and_bounds_retries(monkeypatch) -> None:
    import trader.download.infra.baostock_sync_supplier as supplier_module

    clock = _Clock()
    supplier = BaoStockHistorySupplier(HistorySyncConfiguration(), monotonic=clock, price_total_seconds=180.0)
    attempts = []

    def request(command, state, attempt, timeout):
        attempts.append(attempt.number)
        clock.value += 100.0
        return supplier_module._AttemptResult(None, state, "supplier_daily_qfq_timeout")

    monkeypatch.setattr(supplier, "_request_once", request)
    monkeypatch.setattr(supplier, "close", lambda: None)
    security = BaoStockSecurity("600001", "fixture", "main", date(2000, 1, 1), None, "fixture")
    with pytest.raises(RuntimeError, match="supplier_price_total_timeout"):
        supplier.fetch_code(security, (date(2026, 10, 9),))
    assert attempts == [1, 2]
    clock.value = 179.0
    state = supplier._handle_activity(
        _Activity("supplier_daily_raw", "started"), _RequestState("supplier_daily_raw", None, 0), _Attempt(1, 3), 45
    )
    clock.value = 180.0
    with pytest.raises(RuntimeError, match="supplier_price_total_timeout"):
        supplier._receive(None, state, _Attempt(1, 3))


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


def test_industry_date_progress_keeps_counts_and_elapsed_across_page_activity() -> None:
    clock = _Clock()
    recorder = _ProgressRecorder()
    supplier = BaoStockHistorySupplier(HistorySyncConfiguration(), progress=recorder, monotonic=clock)
    attempt = _Attempt(2, 3)
    state = _RequestState("supplier_industry", "2020-07-01", 0)
    state = supplier._handle_industry_progress(
        HistorySyncProgress("supplier_industry", "started", 4, 18, "2020-07-01"), state, attempt, 45
    )
    clock.value = 3
    state = supplier._handle_activity(_Activity("supplier_industry", "started", "2020-07-01"), state, attempt, 45)
    clock.value = 8
    supplier._publish_failure("supplier_industry", attempt, "2020-07-01", 5, "supplier_industry_timeout")
    assert (recorder.values[-1].completed_units, recorder.values[-1].total_units) == (4, 18)
    supplier._handle_industry_progress(
        HistorySyncProgress("supplier_industry", "completed", 5, 18, "2020-07-01", cache_hit=False), state, attempt, 45
    )
    assert recorder.values[-1].call_elapsed_seconds == 8
    assert recorder.values[-1].attempt == 2


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
        supplier.load_context(date(2026, 9, 11), 2000, universe=())

    waiting = [item for item in recorder.values if item.state == "waiting"]
    assert waiting
    assert all(item.stage == "supplier_calendar" for item in waiting)
    assert waiting[-1].call_elapsed_seconds >= 0.75
    assert recorder.values[-1].state == "failed"
    assert recorder.values[-1].stage == "supplier_calendar"
    assert recorder.values[-1].failure_reason == "supplier_calendar_timeout"


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
        supplier.load_context(date(2026, 9, 11), 2000, universe=())

    assert recorder.values[-1].state == "failed"
    assert recorder.values[-1].call_elapsed_seconds == pytest.approx(1.0)


def test_supplier_keeps_context_and_price_request_policies_separate(monkeypatch) -> None:
    from trader.download.infra import baostock_sync_supplier as supplier_module

    supplier = BaoStockHistorySupplier(
        HistorySyncConfiguration(
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


def test_supplier_login_timeout_does_not_repeat_the_slow_start(monkeypatch) -> None:
    from trader.download.infra import baostock_sync_supplier as supplier_module

    supplier = BaoStockHistorySupplier(
        HistorySyncConfiguration(supplier_timeout_seconds=45.0, supplier_retries=2),
    )
    attempts = []

    def fail(_command, state, attempt, _timeout_seconds):
        attempts.append(attempt.number)
        return supplier_module._AttemptResult(None, state, "supplier_login_timeout")

    monkeypatch.setattr(supplier, "_request_once", fail)
    with pytest.raises(RuntimeError, match="supplier_login_timeout"):
        supplier.load_context(date(2026, 10, 9), 2000, universe=())

    assert attempts == [1]


@pytest.mark.parametrize(("restart_session", "expected_closes"), ((False, 0), (True, 1)))
def test_price_failure_restarts_only_an_unhealthy_supplier_session(
    monkeypatch: pytest.MonkeyPatch,
    restart_session: bool,
    expected_closes: int,
) -> None:
    from trader.download.infra import baostock_sync_supplier as supplier_module

    supplier = BaoStockHistorySupplier(
        HistorySyncConfiguration(supplier_retries=0),
        price_policy=BaoStockRequestPolicy(20.0, 0),
    )
    closes = 0

    def fail(_command, state, _attempt, _timeout_seconds):
        response = supplier_module._Response(
            failure_reason="supplier_query_session_failed" if restart_session else "qfq_daily_query_failed",
            restart_session=restart_session,
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


@pytest.mark.parametrize(("timeout_seconds", "retries"), ((float("nan"), 0), (0.0, 0), (20.0, -1), (20.0, 3)))
def test_supplier_request_policy_rejects_unbounded_values(timeout_seconds: float, retries: int) -> None:
    with pytest.raises(ValueError, match="request policy"):
        BaoStockRequestPolicy(timeout_seconds, retries)


def test_worker_context_keeps_official_universe_without_querying_stock_basic(monkeypatch) -> None:
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
            calls.append("industry")
            return _Result(
                ("updateDate", "code", "industry", "industryClassification"),
                (("2026-10-08", "sz.302132", "fixture", "fixture"), ("2026-10-08", "sh.600002", "old", "fixture")),
            )

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
    assert tuple(item.code for item in context.industry_intervals) == ("302132",)
    assert calls == ["login", "calendar", "industry", "industry", "logout"]
    assert connection.closed

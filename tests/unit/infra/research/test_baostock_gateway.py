from types import SimpleNamespace

import pytest

from trader.domain.research.baostock_daily import BaoStockDailySpec
from trader.infra.research.baostock_daily import BaoStockRowGateway
from trader.infra.research.baostock_history_runtime import _RateLimitedBaoStockSdk, _login


class _Result:
    def __init__(self, fields, rows):
        self.error_code = "0"
        self.error_msg = "success"
        self.fields = fields
        self._rows = iter(rows)
        self._current = None

    def next(self):
        try:
            self._current = next(self._rows)
        except StopIteration:
            return False
        return True

    def get_row_data(self):
        return self._current

    def get_data(self):
        raise AssertionError("get_data must never be used")


class _Sdk:
    __version__ = "0.9.3"

    def query_trade_dates(self, **_kwargs):
        return _Result(("calendar_date", "is_trading_day"), (("2026-08-29", "1"), ("2026-08-30", "1")))

    def query_stock_basic(self):
        return _Result(
            ("code", "code_name", "ipoDate", "outDate", "type", "status"),
            (("sh.600001", "A", "2020-01-01", "", "1", "1"),),
        )

    def query_history_k_data_plus(self, code, fields, **kwargs):
        assert code == "sh.600001"
        adjustment = "3" if kwargs["adjustflag"] == "3" else "2"
        rows = (
            (
                "2026-08-29",
                code,
                "10",
                "10.5",
                "9.8",
                "10.2",
                "9.9",
                "100",
                "1000",
                adjustment,
                "1.2",
                "1",
                "3.03",
            ),
            (
                "2026-08-30",
                code,
                "10",
                "10.5",
                "9.8",
                "10.2",
                "9.9",
                "100",
                "1000",
                adjustment,
                "1.2",
                "1",
                "3.03",
            ),
        )
        return _Result(tuple(fields.split(",")), rows)


def test_gateway_consumes_only_baostock_row_iteration_boundary() -> None:
    gateway = BaoStockRowGateway(_Sdk(), python_version="3.14.0", dependency_versions=(("pandas", "2.3.0"),))
    spec = BaoStockDailySpec(sessions=2)

    calendar = gateway.fetch_calendar(spec)
    universe = gateway.fetch_universe(spec)
    batch = gateway.fetch_code_batch(spec, universe[0], calendar)

    assert len(calendar.open_dates) == 2
    assert universe[0].code == "600001"
    assert len(batch.cells) == 2
    assert all(cell.status == "complete" for cell in batch.cells)
    assert batch.cells[0].unadjusted is not None
    assert batch.cells[0].unadjusted.pct_change == pytest.approx(0.0303)
    assert batch.cells[0].unadjusted.turnover == pytest.approx(0.012)


def test_sdk_queries_are_started_at_most_once_per_second() -> None:
    sdk = _Sdk()
    now = [0.0]
    delays: list[float] = []

    def advance(seconds: float) -> None:
        delays.append(seconds)
        now[0] += seconds

    limited = _RateLimitedBaoStockSdk(sdk, monotonic=lambda: now[0], sleep=advance)
    limited.query_trade_dates(start_date="2026-08-29", end_date="2026-08-30")
    limited.query_stock_basic()
    limited.query_history_k_data_plus(
        "sh.600001",
        "date,code,open,high,low,close,preclose,volume,amount,adjustflag,turn,tradestatus,pctChg",
        start_date="2026-08-29",
        end_date="2026-08-30",
        frequency="d",
        adjustflag="3",
    )

    assert delays == [1.0, 1.0]


class _LoginSdk:
    def __init__(self, error_code: str = "0") -> None:
        self.error_code = error_code
        self.credentials: tuple[str, str] | None = None
        self.api_key = ""

    def login(self, user_id: str = "anonymous", password: str = "123456") -> SimpleNamespace:
        self.credentials = (user_id, password)
        return SimpleNamespace(error_code=self.error_code)

    def set_API_key(self, api_key: str) -> None:
        self.api_key = api_key


def test_login_uses_explicit_credentials_and_optional_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BAOSTOCK_USER_ID", "research-user")
    monkeypatch.setenv("BAOSTOCK_PASSWORD", "research-password")
    monkeypatch.setenv("BAOSTOCK_API_KEY", "research-api-key")
    sdk = _LoginSdk()

    _login(sdk)  # type: ignore[arg-type]

    assert sdk.credentials == ("research-user", "research-password")
    assert sdk.api_key == "research-api-key"


def test_login_preserves_legacy_anonymous_call_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BAOSTOCK_USER_ID", raising=False)
    monkeypatch.delenv("BAOSTOCK_PASSWORD", raising=False)
    monkeypatch.delenv("BAOSTOCK_API_KEY", raising=False)

    class _AnonymousOnlySdk:
        def login(self) -> SimpleNamespace:
            return SimpleNamespace(error_code="0")

    sdk = _AnonymousOnlySdk()

    _login(sdk)  # type: ignore[arg-type]


def test_login_maps_blacklist_and_sdk_socket_bug_to_controlled_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BAOSTOCK_USER_ID", raising=False)
    monkeypatch.delenv("BAOSTOCK_PASSWORD", raising=False)
    monkeypatch.delenv("BAOSTOCK_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="supplier_login_failed_blacklisted"):
        _login(_LoginSdk("10001011"))  # type: ignore[arg-type]

    class _BrokenLoginSdk(_LoginSdk):
        def login(self, user_id: str = "anonymous", password: str = "123456") -> SimpleNamespace:
            raise UnboundLocalError("mySockect")

    with pytest.raises(RuntimeError, match="supplier_login_transport_failed"):
        _login(_BrokenLoginSdk())  # type: ignore[arg-type]

from datetime import date, timedelta
from types import SimpleNamespace


class SparseRangeSdk:
    __version__ = "fixture"

    def __init__(self):
        self.calls = []

    def query_history_k_data_plus(self, code, fields, start_date, end_date, **kwargs):
        start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
        self.calls.append((start, end, kwargs["adjustflag"]))
        rows = []
        for offset in range((end - start).days + 1):
            day = start + timedelta(days=offset)
            price = str(day.day)
            rows.append(
                (
                    day.isoformat(),
                    code,
                    price,
                    price,
                    price,
                    price,
                    price,
                    "100",
                    "1000",
                    kwargs["adjustflag"],
                    "1",
                    "1",
                    "0",
                    "0",
                )
            )
        return _Result(tuple(fields.split(",")), rows)


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

    def query_history_k_data_plus(self, code, fields, start_date, end_date, **kwargs):
        assert code == "sh.600001"
        assert start_date in {"2026-08-29", "2026-08-30"}
        assert end_date == "2026-08-30"
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
                "0",
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
                "1",
            ),
        )
        return _Result(tuple(fields.split(",")), tuple(row for row in rows if row[0] >= start_date))

class _LoginSdk:
    def __init__(self, error_code: str = "0") -> None:
        self.error_code = error_code

    def login(self) -> SimpleNamespace:
        return SimpleNamespace(error_code=self.error_code)

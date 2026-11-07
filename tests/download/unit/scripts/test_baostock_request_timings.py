from datetime import date

import pytest

from scripts.core.catalog import SelectionArguments
from scripts.core.contracts import DiagnosticResult
from scripts.core.projections import BuildReport
from scripts.download.history import baostock_concurrency as probe
from tests.helpers.shared.tool_cli import BuildToolCommands, ParseBusinessOptions, ParseToolArguments


def test_request_trace_separates_initial_query_and_empty_tail_page(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [0.0]
    trace = probe._RequestTrace()
    trace.code = "000708"
    monkeypatch.setattr(probe.time, "monotonic", lambda: clock[0])

    class Rows:
        error_code = "0"
        fields = ("date", "code")
        count = 0

        def next(self) -> bool:
            if self.count == 2000:
                trace.activity("started")
                clock[0] += 2.5
                trace.activity("completed")
                return False
            return True

        def get_row_data(self) -> tuple[str, str]:
            self.count += 1
            return "2026-11-06", "sz.000708"

    class Sdk:
        def query_history_k_data_plus(self, *args, **kwargs):
            trace.activity("started")
            clock[0] += 1.25
            trace.activity("completed")
            return Rows()

    rows = probe._query_one(Sdk(), "000708", date(2018, 1, 1), date(2026, 11, 6), "3", trace=trace)
    assert len(rows) == 2000
    query, page, read = trace.events
    assert (query.phase, query.network_ms, query.elapsed_ms) == ("raw_query", 1250.0, 1250.0)
    assert (page.phase, page.network_ms, page.elapsed_ms, page.rows, page.outcome) == (
        "raw_next_page", 2500.0, 2500.0, 2000, "empty",
    )
    assert (read.phase, read.rows, read.elapsed_ms) == ("raw_read", 2000, 2500.0)


def test_exact_sessions_uses_one_calendar_and_only_requested_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    class Calendar:
        error_code = "0"
        fields = ("calendar_date", "is_trading_day")

        def __init__(self):
            self.rows = iter((("2026-11-04", "1"), ("2026-11-05", "1"), ("2026-11-06", "1")))

        def next(self):
            self.row = next(self.rows, None)
            return self.row is not None

        def get_row_data(self):
            return self.row

    class Sdk:
        def query_trade_dates(self, **kwargs):
            calls.append("calendar")
            return Calendar()

    def one(sdk, code, *, days, trace, window):
        calls.append((code, window))
        return probe.CodeResult(True, 2, 2, True, 0, 0, 0, None, 1.0, tuple(trace.events))

    monkeypatch.setattr(probe, "_open", Sdk)
    monkeypatch.setattr(probe, "_close", lambda sdk: None)
    monkeypatch.setattr(probe, "RateLimitedBaoStockSdk", lambda sdk, **kwargs: sdk)
    monkeypatch.setattr(probe, "_one", one)
    results = probe._serial(probe.WorkerCommand(("000708", "600519"), 61, 45.0, 1.5, 2))
    assert all(result.ok for result in results)
    assert calls == [
        "calendar",
        ("000708", (date(2026, 11, 5), date(2026, 11, 6))),
        ("600519", (date(2026, 11, 5), date(2026, 11, 6))),
    ]
    options = ParseBusinessOptions(ParseToolArguments([
        *SelectionArguments("baostock-concurrency"), "--baostock-serial-only", "--baostock-sessions", "2000",
    ]))
    argv = BuildToolCommands(options)[0].argv
    assert argv[argv.index("--sessions") + 1] == "2000"


def test_public_request_timings_keep_only_measurement_fields() -> None:
    report = BuildReport("baostock-concurrency", (DiagnosticResult("baostock_concurrency", 0, 1, {
        "status": "passed", "history_trading_sessions": 2000,
        "experiments": [{"request_timings": [{
            "phase": "raw_next_page", "rows": 2000, "elapsed_ms": 100.0,
            "outcome": "empty", "raw_payload": "private", "vendor_code": "10001011",
        }]}],
    }, None),))
    check = report["checks"][0]
    assert check["history_trading_sessions"] == 2000
    timing = check["experiments"][0]["request_timings"][0]
    assert timing["phase"] == "raw_next_page" and timing["rows"] == 2000
    assert timing["vendor_code"] == "10001011"
    assert "raw_payload" not in timing


@pytest.mark.parametrize("value,expected", [("10001011", "10001011"), ("0", "0"), ("secret", None), ("１２", None)])
def test_vendor_codes_only_accept_short_ascii_digits(value, expected) -> None:
    trace = probe._RequestTrace()
    trace.record("raw_query", probe.time.monotonic(), 0, "failed", vendor_code=value)
    assert trace.events[0].vendor_code == expected


@pytest.mark.parametrize("complete_facts", [True, False])
def test_production_probe_uses_real_typed_supplier_boundary_and_isolated_paths(
    monkeypatch: pytest.MonkeyPatch, complete_facts: bool,
) -> None:
    from types import SimpleNamespace

    from trader.download.domain.baostock_daily import BaoStockSecurity
    from trader.download.infra import baostock_sync_supplier, download_settings

    dates = (date(2026, 11, 5), date(2026, 11, 6))
    calls = []
    load_configuration = download_settings.LoadHistorySyncConfiguration

    def configuration(root, paths):
        assert str(paths.root).startswith("/tmp/trader-baostock-production-")
        calls.append("configuration")
        return load_configuration(root, paths)

    class Supplier:
        def __init__(self, config, **kwargs):
            assert kwargs["price_total_seconds"] == 45.0
            calls.append("supplier")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            calls.append("close")

        def load_context(self, as_of, sessions, *, universe):
            assert sessions == 2
            assert all(isinstance(security, BaoStockSecurity) for security in universe)
            calls.append("calendar")
            return SimpleNamespace(calendar=SimpleNamespace(open_dates=dates))

        def fetch_code(self, security, selected):
            assert selected == dates
            calls.append(security.code)
            return SimpleNamespace(
                batch=SimpleNamespace(
                    cells=tuple(SimpleNamespace(trade_date=day, status="complete", unadjusted=object(), qfq=object())
                                for day in dates),
                    duplicate_rows=0, null_rows=0, out_of_window_rows=0, future_rows=0, failure_reasons=(),
                ),
                daily_facts=tuple(SimpleNamespace(trade_date=day) for day in (dates if complete_facts else dates[:1])),
            )

    monkeypatch.setattr(download_settings, "LoadHistorySyncConfiguration", configuration)
    monkeypatch.setattr(baostock_sync_supplier, "BaoStockHistorySupplier", Supplier)
    results = probe._production(probe.WorkerCommand(("000708",), 61, 45.0, 1.5, 2))
    assert results[0].ok is complete_facts
    assert results[0].raw_rows == results[0].qfq_rows == 2
    assert results[0].error == (None if complete_facts else "production_batch_incomplete")
    assert results[0].request_policy == probe.ProbeRequestPolicy(45.0, 45.0, 1.5, 2)
    total = results[0].timings[-1]
    assert total.phase == "production_total"
    assert total.elapsed_ms == results[0].elapsed_ms
    assert total.rows == total.raw_rows == total.qfq_rows == 2
    assert calls == ["configuration", "supplier", "calendar", "000708", "close"]
    options = ParseBusinessOptions(ParseToolArguments([
        *SelectionArguments("baostock-concurrency"), "--baostock-serial-only", "--baostock-sessions", "2000",
        "--baostock-production", "--baostock-sizes", "1",
    ]))
    assert "--production" in BuildToolCommands(options)[0].argv
    with pytest.raises(ValueError, match="production"):
        ParseBusinessOptions(ParseToolArguments([
            *SelectionArguments("baostock-concurrency"), "--baostock-serial-only", "--baostock-production",
        ]))

from datetime import date, timedelta

import pytest

from tests.unit.infra.research.test_history_archive_sync import NOW
from tests.unit.infra.research.test_history_industry_cache import DATES, UNIVERSE, _IndustrySdk
from tests.unit.infra.test_history_supplier_router import Prices, StSource
from trader.download.domain.baostock_daily import BaoStockCalendar
from trader.download.domain.history_sync import HistorySyncConfiguration
from trader.download.infra import baostock_sync_supplier as supplier_module
from trader.download.infra.baostock_gateway import BaoStockRowGateway
from trader.download.infra.history_archive_sync import run_history_sync
from trader.download.infra.history_control_index import HistoryControlIndex
from trader.download.infra.history_industry_cache import HistoryIndustryQueryCache
from trader.download.infra.history_reference_files import history_reference_path
from trader.download.infra.history_snapshot_reader import HistorySnapshotReader
from trader.download.infra.history_supplier_router import HistorySupplierRouter


@pytest.fixture
def industry_source(monkeypatch):
    sdk = _IndustrySdk()
    monkeypatch.setattr(supplier_module, "load_baostock_sdk", lambda: sdk)
    gateway = BaoStockRowGateway(sdk)
    requests = []

    def request(self, command):
        assert isinstance(command, supplier_module._FetchIndustry)
        requests.append(command.day)
        return supplier_module._Response(industry_facts=gateway.fetch_industry_facts(command.day))

    monkeypatch.setattr(supplier_module.BaoStockHistorySupplier, "_request", request)
    return sdk, requests


def test_warm_industry_context_does_not_start_or_query_supplier(tmp_path, monkeypatch, industry_source):
    sdk, requests = industry_source
    cache = HistoryIndustryQueryCache(tmp_path / "industry.sqlite3")
    calendar = BaoStockCalendar(DATES)
    with supplier_module.BaoStockHistorySupplier(HistorySyncConfiguration()) as source:
        cold = source.load_industry_context(calendar, UNIVERSE, industry_cache=cache)
    assert requests == list(DATES)
    assert len(sdk.calls) == len(DATES)

    def unexpected(*args, **kwargs):
        pytest.fail("warm industry evidence must not start or query a supplier")

    monkeypatch.setattr(supplier_module.BaoStockHistorySupplier, "_request", unexpected)
    monkeypatch.setattr(supplier_module.BaoStockHistorySupplier, "_start", unexpected)
    with supplier_module.BaoStockHistorySupplier(HistorySyncConfiguration()) as source:
        assert source.load_industry_context(calendar, UNIVERSE, industry_cache=cache) == cold


def test_real_request_wrapper_has_one_start_and_completion_per_industry_date(tmp_path, monkeypatch, capsys):
    from trader.download.entrypoints.history_sync_progress import StderrHistorySyncProgress

    sdk = _IndustrySdk()
    monkeypatch.setattr(supplier_module, "load_baostock_sdk", lambda: sdk)
    gateway = BaoStockRowGateway(sdk)

    def request_once(self, command, state, attempt, timeout):
        self._publish(state.stage, "started", attempt, state.current_item, 0)
        state = self._handle_activity(
            supplier_module._Activity("supplier_industry", "started", str(command.day)),
            state,
            attempt,
            timeout,
        )
        return supplier_module._AttemptResult(
            supplier_module._Response(industry_facts=gateway.fetch_industry_facts(command.day)),
            state,
            None,
        )

    monkeypatch.setattr(supplier_module.BaoStockHistorySupplier, "_request_once", request_once)
    cache = HistoryIndustryQueryCache(tmp_path / "industry.sqlite3")
    with supplier_module.BaoStockHistorySupplier(
        HistorySyncConfiguration(),
        progress=StderrHistorySyncProgress(),
    ) as source:
        source.load_industry_context(BaoStockCalendar(DATES), UNIVERSE, industry_cache=cache)
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 2 * len(DATES)
    for day in DATES:
        dated = [line for line in lines if str(day) in line]
        assert len(dated) == 2
        assert "开始" in dated[0] and "完成" in dated[1] and "供应商查询" in dated[1]


def test_future_observation_cannot_be_reused_at_an_earlier_cutoff(tmp_path, industry_source):
    _, requests = industry_source
    cache = HistoryIndustryQueryCache(tmp_path / "industry.sqlite3")
    with supplier_module.BaoStockHistorySupplier(HistorySyncConfiguration()) as source:
        source.load_industry_context(BaoStockCalendar(DATES), UNIVERSE, industry_cache=cache)
        requests.clear()
        source.load_industry_context(BaoStockCalendar(DATES[:-1]), UNIVERSE, industry_cache=cache)
    assert requests == list(DATES[:-1])


def test_new_cutoff_fetches_only_new_query_after_more_than_thirty_days(tmp_path, industry_source):
    _, requests = industry_source
    cache = HistoryIndustryQueryCache(tmp_path / "industry.sqlite3")
    with supplier_module.BaoStockHistorySupplier(HistorySyncConfiguration()) as source:
        source.load_industry_context(BaoStockCalendar(DATES), UNIVERSE, industry_cache=cache)
        requests.clear()
        next_cutoff = DATES[-1] + timedelta(days=60)
        source.load_industry_context(BaoStockCalendar((*DATES, next_cutoff)), UNIVERSE, industry_cache=cache)
    assert requests == [next_cutoff]


@pytest.mark.parametrize("damage", ("missing", "tampered"))
def test_cached_industry_repairs_reference_without_supplier_or_price_requests(tmp_path, industry_source, damage):
    _, requests = industry_source
    dates = (date(2026, 9, 8), date(2026, 9, 9))
    config = HistorySyncConfiguration(tmp_path / "history", sessions=2, reread_sessions=2, minimum_free_bytes=0)
    cache = HistoryIndustryQueryCache(tmp_path / "industry.sqlite3")
    prices = Prices()
    prices.dates = dates
    with supplier_module.BaoStockHistorySupplier(config) as source:
        router = HistorySupplierRouter(source, prices, lambda: UNIVERSE, StSource(), cache)
        assert run_history_sync(config, router, clock=lambda: NOW).state == "completed"
        control = HistoryControlIndex(config.archive_root / "control.sqlite3")
        snapshot = control.load_published_state().snapshot
        identity = control.read_source(snapshot.source_identity_hash)
        path = history_reference_path(config.archive_root, identity.supplier_contract[12:76])
        original = path.read_bytes()
        if damage == "missing":
            path.unlink()
        else:
            path.write_text("{}")
        requests.clear()
        prices.calls.clear()
        assert run_history_sync(config, router, clock=lambda: NOW).state == "already_current"
    assert requests == [] and prices.calls == []
    assert path.read_bytes() == original
    assert control.load_published_state().snapshot == snapshot
    assert HistorySnapshotReader(config.archive_root).reference_index(snapshot).industry_on("600001", dates[-1])

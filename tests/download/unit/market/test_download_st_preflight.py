"""Every download filters current and historical ST before business requests."""

from tests.helpers.shared.configuration import SqlitePolicyFixture

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from threading import enumerate as enumerate_threads

import pytest

from tests.helpers.download.history.qfq_windows import _window
from tests.helpers.download.history.infra.history_sync import NOW
from tests.helpers.download.history.history_supplier_router import Baseline, _current_universe
from trader import bootstrap
from trader.download.domain.baostock_daily import BaoStockCalendar
from trader.download.domain.history_reference import HistoryStEvidence
from tests.helpers.download.configuration import HistorySyncConfigurationFixture as HistorySyncConfiguration
from trader.download.infra.reference_index import SQLiteReferenceIndex
from trader.download.infra.tencent_qfq_supplier import TencentQfqSupplier
from trader.infra.http_contracts import HttpError
from trader.infra.settings import LoadRuntimeSettings
from trader.infra.settings.data_paths import DataPaths

_ROOT = Path(__file__).resolve().parents[4]
_DAYS = (NOW.date() - timedelta(days=2), NOW.date() - timedelta(days=1), NOW.date())


@pytest.fixture
def st_sources(monkeypatch):
    calls = []
    sessions = []
    universe = _current_universe(*(f"60000{index}" for index in range(1, 8)))
    universe = tuple(replace(item, name="*ST公司") if item.code == "600002" else item for item in universe)

    class Response:
        def __init__(self, code):
            names = "曾ST公司" if code in {"600003", "600006"} else "正常公司"
            self.content = ("查询失败" if code == "600004" else f"<td>证券简称更名历史：</td><td>{names}</td>").encode(
                "gb18030"
            )

        def raise_for_status(self):
            pass

        def close(self):
            pass

    class Session:
        def __init__(self, *, identity_content_encodings=()):
            assert identity_content_encodings in ((), ("normal",))
            self.closed = False
            sessions.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.closed = True

        def get(self, url, **kwargs):
            assert not self.closed
            if url == "https://www.capco.org.cn/":
                assert set(calls) == {"600004", "600005", "600006", "600007"}
                raise HttpError("fixture_official_industry_unavailable")
            assert "vCI_CorpInfo/stockid/" in url
            assert kwargs["timeout"] == 15.0
            code = url.rsplit("/", 1)[1].split(".", 1)[0]
            assert code != "600002", "official ST names must be excluded before per-stock requests"
            calls.append(code)
            return Response(code)

    monkeypatch.setattr(bootstrap, "LoadCurrentAShareUniverse", lambda *_args, **_kwargs: universe)
    monkeypatch.setattr(bootstrap, "OpenExternalHttp", Session)
    monkeypatch.setattr(bootstrap, "_utc_now", lambda: NOW)
    monkeypatch.setattr(
        bootstrap,
        "FetchAkshareCalendar",
        lambda *_args, **_kwargs: tuple(NOW.date() - timedelta(days=offset) for offset in range(250, -1, -1)),
    )
    monkeypatch.setattr(bootstrap.CurrentIndustryPublication, "fetch", lambda self, at, universe, calendar: ())
    monkeypatch.setattr(
        bootstrap,
        "LoadHistorySyncConfiguration",
        lambda root, paths: HistorySyncConfiguration(paths.history, paths.reference, minimum_free_bytes=0),
    )
    return calls, sessions


def _seed_st(path):
    SQLiteReferenceIndex(path, sqlite_policy=SqlitePolicyFixture()).merge_st(
        (
            HistoryStEvidence("600001", NOW.date(), "clear"),
            HistoryStEvidence("600002", NOW.date(), "clear"),
            HistoryStEvidence("600003", NOW.date() - timedelta(days=1), "ever_st"),
            HistoryStEvidence("600004", NOW.date(), "unknown"),
            HistoryStEvidence("600006", NOW.date() - timedelta(days=1), "clear"),
            HistoryStEvidence("600007", NOW.date() - timedelta(days=1), "clear"),
        ),
    )




def test_price_commands_complete_st_filter_before_calendar_and_prices(
    tmp_path, monkeypatch, st_sources
):
    calls, sessions = st_sources
    reference_root = DataPaths(tmp_path / "data").reference
    _seed_st(reference_root / "company_st.sqlite3")
    downloaded = []

    class Prices(TencentQfqSupplier):
        def load_calendar(self, _as_of, _sessions):
            assert set(calls) == {"600004", "600005", "600006", "600007"}
            return BaoStockCalendar(_DAYS)

        def fetch_window(self, security, dates, **_options):
            assert security.code in {"600001", "600004", "600005", "600007"}
            downloaded.append(security.code)
            return _window(security.code, dates)

    class Industry(Baseline):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def load_context(self, *_args, **_options):
            raise AssertionError("BaoStock must not prepare history references")

    monkeypatch.setattr(bootstrap, "TencentQfqSupplier", Prices)
    monkeypatch.setattr(bootstrap, "BaoStockHistorySupplier", lambda *_args, **_kwargs: Industry(_DAYS))
    result = bootstrap.ExecuteHistoryDownload(
        HistorySyncConfiguration(
            tmp_path / "history", reference_root, sessions=3, reread_sessions=2, minimum_free_bytes=0
        ),
        shutdown_timeout_seconds=2.0,
        clock=lambda: NOW, sqlite_policy=SqlitePolicyFixture(),
    )
    assert result.state == "completed", result.reason
    assert set(downloaded) == {"600001", "600004", "600005", "600007"}
    assert all(session.closed for session in sessions)


def test_corrupt_st_cache_stops_risk_commands_before_business_requests(tmp_path, monkeypatch, st_sources):
    calls, sessions = st_sources
    settings = replace(
        LoadRuntimeSettings(_ROOT / "config/runtime.json"),
        project_root=tmp_path,
        runtime_dir=tmp_path / "runtime",
        data_paths=DataPaths(tmp_path / "data"),
    )
    settings.data_paths.reference.mkdir(parents=True)
    (settings.data_paths.reference / "company_st.sqlite3").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(bootstrap, "LoadRuntimeSettings", lambda _path: settings)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("business preparation must wait for valid ST evidence")

    monkeypatch.setattr(bootstrap, "ChinaTradingCalendar", forbidden)
    result = bootstrap.ExecuteSync(Path("unused"), "history")
    assert result.exit_code == 1 and not result.tasks
    assert calls == []
    assert all(session.closed for session in sessions)
    assert not any(thread.name.startswith("risk-st-check") for thread in enumerate_threads())
    assert not (tmp_path / "data/risk").exists()

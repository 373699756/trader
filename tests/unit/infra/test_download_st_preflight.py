"""Every download filters current and historical ST before business requests."""

from tests.helpers.configuration import DownloadFailureConfigurationFixture, SqlitePolicyFixture

from dataclasses import replace
from datetime import timedelta
from functools import partial
from io import StringIO
from pathlib import Path
from threading import enumerate as enumerate_threads

import pytest

from tests.component.test_qfq_windows import _window
from tests.unit.infra.research.test_history_sync import NOW
from tests.unit.infra.test_history_supplier_router import Baseline, _current_universe
from trader import bootstrap
from trader.download.domain.baostock_daily import BaoStockCalendar
from trader.download.domain.company_evidence import CompanyEvidenceSyncResult
from trader.download.domain.history_reference import HistoryStEvidence
from tests.helpers.configuration import HistorySyncConfigurationFixture as HistorySyncConfiguration
from trader.download.entrypoints.company_evidence_progress import CompanyEvidenceProgress
from trader.download.infra.download_settings import LoadCompanySyncConfiguration, LoadQfqWindowConfiguration
from trader.download.infra.reference_index import SQLiteReferenceIndex
from trader.download.infra.qfq_sqlite import SQLiteQfqWindowCache
from trader.download.infra.tencent_qfq_supplier import TencentQfqSupplier
from trader.infra.settings import LoadRuntimeSettings
from trader.infra.settings.data_paths import DataPaths

_ROOT = Path(__file__).resolve().parents[3]
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
        def __init__(self):
            self.closed = False
            sessions.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.closed = True

        def get(self, url, **kwargs):
            assert not self.closed
            assert "vCI_CorpInfo/stockid/" in url
            assert kwargs["timeout"] == 15.0
            code = url.rsplit("/", 1)[1].split(".", 1)[0]
            assert code != "600002", "official ST names must be excluded before per-stock requests"
            calls.append(code)
            return Response(code)

    monkeypatch.setattr(bootstrap, "LoadCurrentAShareUniverse", lambda *_args: universe)
    monkeypatch.setattr(bootstrap, "OpenExternalHttp", Session)
    monkeypatch.setattr(bootstrap, "_utc_now", lambda: NOW)
    monkeypatch.setattr(
        bootstrap,
        "FetchAkshareCalendar",
        lambda *_args, **_kwargs: tuple(NOW.date() - timedelta(days=offset) for offset in range(250, -1, -1)),
    )
    monkeypatch.setattr(bootstrap.CurrentIndustryPublication, "fetch", lambda self, at: ())
    monkeypatch.setattr(
        bootstrap,
        "LoadHistorySyncConfiguration",
        lambda root, paths: HistorySyncConfiguration(paths.history, paths.reference, minimum_free_bytes=0),
    )
    company_config = LoadCompanySyncConfiguration(_ROOT)
    monkeypatch.setattr(bootstrap, "LoadCompanySyncConfiguration", lambda _root: company_config)
    monkeypatch.setattr(bootstrap, "LoadDownloadFailureConfiguration", lambda _root: DownloadFailureConfigurationFixture())
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


@pytest.mark.parametrize("existing_cache", (False, True))
def test_risk_commands_refresh_st_then_download_only_confirmed_codes(tmp_path, monkeypatch, st_sources, existing_cache):
    calls, sessions = st_sources
    settings = replace(
        LoadRuntimeSettings(_ROOT / "config/runtime.json"),
        project_root=tmp_path,
        runtime_dir=tmp_path / "runtime",
        data_paths=DataPaths(tmp_path / "data"),
    )
    monkeypatch.setattr(bootstrap, "LoadRuntimeSettings", lambda _path: settings)
    evidence_path = settings.data_paths.reference / "company_st.sqlite3"
    if existing_cache:
        _seed_st(evidence_path)
    events = []
    expected_queries = {"600004", "600005", "600006", "600007"}
    if not existing_cache:
        expected_queries |= {"600001", "600003"}

    def download(root, progress, request, *, cancel_requested, sqlite_policy, failure_configuration):
        codes, observed_at = request.codes, request.observed_at
        assert request.configuration.source_timeout_seconds == 8.0
        assert set(calls) == expected_queries
        assert all(session.closed for session in sessions)
        assert codes == ("600001", "600005", "600007")
        events.append("business_download")
        return CompanyEvidenceSyncResult(observed_at, len(codes), 3, 0, 0, 0, 0, (), 0.0)

    monkeypatch.setattr(bootstrap, "_sync_company_evidence", download)
    stream = StringIO()
    monkeypatch.setattr(
        "trader.bootstrap.company_evidence_progress.CompanyEvidenceProgress",
        lambda **kwargs: CompanyEvidenceProgress(stream, **kwargs),
    )
    result = bootstrap.ExecuteSync(Path("unused"), "company")
    assert result.tasks[0].company.requested_codes == 3
    assert events == ["business_download"]
    assert set(calls) == expected_queries
    assert {item.code for item in SQLiteReferenceIndex(evidence_path, sqlite_policy=SqlitePolicyFixture()).read_st() if item.status == "ever_st"} == {
        "600002",
        "600003",
        "600006",
    }
    assert "ST资格" in stream.getvalue()
    assert all(session.closed for session in sessions)
    assert not any(thread.name.startswith("risk-st-check") for thread in enumerate_threads())


@pytest.mark.parametrize("command", ("history", "qfq", "market"))
def test_price_commands_complete_st_filter_before_calendar_and_prices(
    tmp_path, monkeypatch, st_sources, command, capsys
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
            assert security.code in {"600001", "600005", "600007"}
            downloaded.append(security.code)
            return _window(security.code, dates)

    class Industry(Baseline):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def load_industry_context(self, calendar, universe, **options):
            assert tuple(item.code for item in universe) == ("600001", "600005", "600007")
            return super().load_industry_context(calendar, universe, **options)

    monkeypatch.setattr(bootstrap, "TencentQfqSupplier", Prices)
    monkeypatch.setattr(bootstrap, "BaoStockHistorySupplier", lambda *_args, **_kwargs: Industry(_DAYS))
    if command == "history":
        result = bootstrap.ExecuteHistoryDownload(
            HistorySyncConfiguration(
                tmp_path / "history", reference_root, sessions=3, reread_sessions=2, minimum_free_bytes=0
            ),
            qfq_configuration=LoadQfqWindowConfiguration(_ROOT),
            shutdown_timeout_seconds=2.0,
            clock=lambda: NOW, sqlite_policy=SqlitePolicyFixture(),
        )
        assert result.state == "completed", result.reason
    elif command == "qfq":
        paths = DataPaths(tmp_path / "data")
        result = bootstrap.ExecuteQfqDownload(
            paths,
            bootstrap.LoadHistorySyncConfiguration(_ROOT, paths),
            LoadQfqWindowConfiguration(_ROOT),
            failure_configuration=DownloadFailureConfigurationFixture(),
            shutdown_timeout_seconds=2.0,
            now=lambda: NOW, sqlite_policy=SqlitePolicyFixture(),
        )
        assert result.completed_codes == 3
        assert not result.needs_retry
    else:
        paths = DataPaths(tmp_path / "data")
        settings = replace(
            LoadRuntimeSettings(_ROOT / "config/runtime.json"), data_paths=paths
        )
        monkeypatch.setattr(bootstrap, "LoadRuntimeSettings", lambda _path: settings)
        monkeypatch.setattr(bootstrap, "ExecuteQfqDownload", partial(bootstrap.ExecuteQfqDownload, now=lambda: NOW))
        result = bootstrap.ExecuteSync(Path("unused"), "market")
        assert result.exit_code == 0, result
        assert result.tasks[0].market.completed_codes == 3
        cache = SQLiteQfqWindowCache(paths.daily_window, "v2", sqlite_policy=SqlitePolicyFixture())
        assert cache.codes() == frozenset({"600001", "600005", "600007"})
        window = cache.read_code("600001")
        assert len(window.cells) == LoadQfqWindowConfiguration(_ROOT).v2_sessions
        assert all(cell.qfq is not None for cell in window.cells)
        output = capsys.readouterr().err
        assert "sync company" not in output
        stages = ("ST资格", "交易日历", "当前行业", "参考发布", "行情更新", "qfq 准备")
        assert [output.index(stage) for stage in stages] == sorted(output.index(stage) for stage in stages)
        assert "sync market | ST资格" in output
        assert "开始更新 raw/qfq 短窗口" in output
    assert set(downloaded) == {"600001", "600005", "600007"}
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
    monkeypatch.setattr(bootstrap, "_sync_company_evidence", forbidden)
    result = bootstrap.ExecuteSync(Path("unused"), "company")
    assert result.exit_code == 1 and not result.tasks
    assert calls == []
    assert all(session.closed for session in sessions)
    assert not any(thread.name.startswith("risk-st-check") for thread in enumerate_threads())
    assert not (tmp_path / "data/risk").exists()

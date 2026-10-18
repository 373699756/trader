"""Every download filters current and historical ST before business requests."""

from dataclasses import replace
from datetime import timedelta
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
from trader.download.domain.history_sync import HistorySyncConfiguration
from trader.download.entrypoints.company_evidence_progress import CompanyEvidenceProgress
from trader.download.infra.history_reference_files import ReadStEvidence, WriteStEvidence
from trader.download.infra.tencent_qfq_supplier import TencentQfqSupplier
from trader.infra.settings import LoadRuntimeSettings

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
    monkeypatch.setattr(bootstrap.requests, "Session", Session)
    monkeypatch.setattr(bootstrap, "_utc_now", lambda: NOW)
    return calls, sessions


def _seed_st(path):
    WriteStEvidence(
        path,
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
        reference_dir=tmp_path / "reference",
    )
    monkeypatch.setattr(bootstrap, "LoadRuntimeSettings", lambda _path: settings)
    evidence_path = settings.reference_dir / "historical_st.json"
    if existing_cache:
        _seed_st(evidence_path)
    events = []
    expected_queries = {"600004", "600005", "600006", "600007"}
    if not existing_cache:
        expected_queries |= {"600001", "600003"}

    def download(root, progress, request, *, timeout_seconds):
        codes, observed_at = request.codes, request.observed_at
        assert set(calls) == expected_queries
        assert all(session.closed for session in sessions)
        assert codes == ("600001", "600005", "600007")
        events.append("business_download")
        return CompanyEvidenceSyncResult(observed_at, len(codes), 3, 0, 0, 0, 0, (), 0.0)

    monkeypatch.setattr(bootstrap, "_sync_company_evidence", download)
    stream = StringIO()
    result = bootstrap.ExecuteRiskDownload("unused", progress=CompanyEvidenceProgress(stream))
    assert result.requested_codes == 3
    assert events == ["business_download"]
    assert set(calls) == expected_queries
    assert {item.code for item in ReadStEvidence(evidence_path) if item.status == "ever_st"} == {
        "600002",
        "600003",
        "600006",
    }
    assert "ST资格" in stream.getvalue()
    assert all(session.closed for session in sessions)
    assert not any(thread.name.startswith("risk-st-check") for thread in enumerate_threads())


@pytest.mark.parametrize("command", ("history", "qfq"))
def test_price_commands_complete_st_filter_before_calendar_and_prices(tmp_path, monkeypatch, st_sources, command):
    calls, sessions = st_sources
    reference_root = tmp_path / "reference"
    _seed_st(reference_root / "historical_st.json")
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
            clock=lambda: NOW,
        )
        assert result.state == "completed", result.reason
    else:
        result = bootstrap.ExecuteQfqDownload(tmp_path, reference_root=reference_root, now=lambda: NOW)
        assert result.completed_codes == 3
        assert not result.needs_retry
    assert set(downloaded) == {"600001", "600005", "600007"}
    assert all(session.closed for session in sessions)


def test_corrupt_st_cache_stops_risk_commands_before_business_requests(tmp_path, monkeypatch, st_sources):
    calls, sessions = st_sources
    settings = replace(
        LoadRuntimeSettings(_ROOT / "config/runtime.json"),
        project_root=tmp_path,
        runtime_dir=tmp_path / "runtime",
        reference_dir=tmp_path / "reference",
    )
    settings.reference_dir.mkdir()
    (settings.reference_dir / "historical_st.json").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(bootstrap, "LoadRuntimeSettings", lambda _path: settings)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("business preparation must wait for valid ST evidence")

    monkeypatch.setattr(bootstrap, "ChinaTradingCalendar", forbidden)
    monkeypatch.setattr(bootstrap, "_sync_company_evidence", forbidden)
    with pytest.raises(ValueError, match="history ST evidence"):
        bootstrap.ExecuteRiskDownload("unused")
    assert calls == []
    assert all(session.closed for session in sessions)
    assert not any(thread.name.startswith("risk-st-check") for thread in enumerate_threads())
    assert not (tmp_path / "data/risk").exists()

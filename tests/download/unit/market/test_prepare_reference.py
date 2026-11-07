"""ST qualification stops reference I/O and all dependent download work early."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tests.helpers.shared.configuration import SqlitePolicyFixture
from tests.helpers.shared.reference_fixtures import PreparedFixture
from trader.download.application.prepare_reference import IndustryDatabaseIncompatibleError, PrepareReference, ReferencePreparationDependencies, ReferencePreparationSources
from trader.download.application.sync_data import SyncData
from trader.download.application.progress_message import ProgressLevel
from trader.download.domain.company_evidence import CompanyEvidenceSyncResult
from trader.download.domain.history_maintenance import HistoryMaintenanceStatus
from trader.download.domain.qfq_window import QfqUpdateResult
from trader.download.infra.reference_index import SQLiteReferenceIndex

_NOW = datetime(2026, 11, 4, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
_SOURCES = ReferencePreparationSources("fixture-ST", "fixture-calendar", "fixture-industry")


@pytest.mark.parametrize("reused", (False, True))
def test_calendar_detail_retains_explicit_provider_on_fetch_and_cache_reuse(tmp_path, reused):
    reference = PreparedFixture(_NOW)
    index = SQLiteReferenceIndex(tmp_path / "reference.sqlite3", sqlite_policy=SqlitePolicyFixture())
    events, calls = [], []
    if reused:
        index.publish(reference, reference.st_evidence)

    def fetch():
        calls.append(True)
        return reference.open_dates

    assert index.calendar(_NOW.date(), fetch=fetch, progress=events.append, source="新浪/Akshare") == reference.open_dates
    assert events and all(event.sources == ("新浪/Akshare",) for event in events)
    assert bool(calls) is not reused
    assert events[-1].status == ("reused" if reused else "complete")


def test_incompatible_industry_reports_its_exact_download_scope_without_stopping_price_preparation():
    previous = PreparedFixture(_NOW)
    reports, published = [], []

    def incompatible(*_args):
        raise IndustryDatabaseIncompatibleError("industry_database_incompatible")

    result = PrepareReference(previous.official_universe, _NOW, ReferencePreparationDependencies(
        lambda *_args: previous.st_evidence, lambda: previous.open_dates, incompatible,
        lambda reference, evidence: published.append(reference), lambda: False,
        _SOURCES,
    ), report=reports.append)
    text = str(reports)
    assert "market/reference/industry.sqlite3" in text
    assert "仅清理" in text and "其他资料同步继续" in text
    assert result.industry_version is None and published == [result]


@dataclass(frozen=True)
class _Industry:
    data_version: str = "fixture-industry"
    received_at: datetime = _NOW


@pytest.mark.parametrize("target", ("all", "history", "market", "company"))
@pytest.mark.parametrize("population", ("ever_st", "unknown", "current_st"))
def test_no_eligible_stock_stops_before_calendar_industry_publication_and_downloads(tmp_path, target, population):
    index = SQLiteReferenceIndex(tmp_path / "company_st.sqlite3", sqlite_policy=SqlitePolicyFixture())
    previous = PreparedFixture(_NOW - timedelta(days=1))
    index.publish(previous, previous.st_evidence)
    before = index.path.read_bytes()
    current = PreparedFixture(_NOW)
    universe = current.official_universe
    evidence = current.st_evidence
    if population == "current_st":
        universe = (replace(universe[0], name="*ST测试"),)
    else:
        evidence = (replace(evidence[0], status=population),)
    calls = []

    def st_evidence(securities, day):
        calls.append("st")
        assert securities == universe and day == _NOW.date()
        return evidence

    def forbidden(*_args):
        pytest.fail("ST-excluded population reached downstream work")

    dependencies = ReferencePreparationDependencies(
        st_evidence,
        forbidden,
        forbidden,
        forbidden,
        lambda: False,
        _SOURCES,
    )
    result = SyncData(
        lambda: PrepareReference(universe, _NOW, dependencies),
        forbidden,
        forbidden,
        forbidden,
        lambda: False,
    ).execute(target)

    assert result.tasks == () and result.exit_code == 1
    assert result.failure == "preparation_ValueError"
    assert calls == ["st"]
    assert index.read() == previous and index.path.read_bytes() == before


def test_mixed_population_uses_one_st_cut_and_keeps_original_supplier_check_dates():
    first = PreparedFixture(_NOW).official_universe[0]
    universe = tuple(replace(first, code=f"60000{number}") for number in range(1, 6))
    universe = (*universe[:3], replace(universe[3], name="ＳＴ测试"), universe[4])
    old_day = (_NOW - timedelta(days=10)).date()
    source = tuple(
        replace(PreparedFixture(_NOW).st_evidence[0], code=item.code, status=status, checked_on=old_day)
        for item, status in zip(universe, ("clear", "ever_st", "unknown", "clear", "clear"), strict=True)
    )
    calls, published, received = [], [], []
    dates = PreparedFixture(_NOW).open_dates

    def st_evidence(securities, day):
        calls.append("st")
        return tuple(reversed(source))

    def calendar():
        calls.append("calendar")
        return dates

    def industry(at, eligible, open_dates):
        calls.append("industry")
        assert tuple(item.code for item in eligible) == ("600001", "600005")
        assert open_dates == dates
        return (_Industry(),)

    def publish(reference, original):
        calls.append("publish")
        published.append((reference, original))

    def record(reference):
        received.append(reference)
        assert tuple(item.code for item in reference.eligible_universe) == ("600001", "600005")

    def history(reference):
        record(reference)
        return HistoryMaintenanceStatus(
            "completed",
            None,
            Path("history"),
            "tencent",
            None,
            "a" * 64,
            _NOW.date(),
            _NOW.date(),
        )

    def market(reference):
        record(reference)
        return QfqUpdateResult(_NOW.date(), 2)

    def company(reference):
        record(reference)
        return CompanyEvidenceSyncResult(
            _NOW,
            2,
            2,
            0,
            0,
            0,
            0,
            (),
            0,
        )

    dependencies = ReferencePreparationDependencies(
        st_evidence,
        calendar,
        industry,
        publish,
        lambda: False,
        _SOURCES,
    )
    result = SyncData(
        lambda: PrepareReference(tuple(reversed(universe)), _NOW, dependencies),
        history,
        market,
        company,
        lambda: False,
    ).execute("all")

    assert result.exit_code == 0 and len(received) == 3
    assert calls == ["st", "calendar", "industry", "publish"]
    reference, original = published[0]
    assert all(item is reference for item in received)
    assert reference.official_universe == universe
    assert reference.industry_version == "fixture-industry"
    assert reference.st_evidence == tuple(replace(item, checked_on=_NOW.date()) for item in source)
    expected = replace(
        PreparedFixture(_NOW, universe, industry_version="fixture-industry"), st_evidence=reference.st_evidence
    )
    assert reference == expected
    assert original == tuple(reversed(source))


@pytest.mark.parametrize("invalid", ("missing", "duplicate", "foreign"))
def test_incomplete_st_cut_stops_before_other_reference_io(invalid):
    reference = PreparedFixture(_NOW)
    evidence = reference.st_evidence
    if invalid == "missing":
        evidence = ()
    elif invalid == "duplicate":
        evidence = (*evidence, evidence[0])
    else:
        evidence = (replace(evidence[0], code="600002"),)

    def forbidden(*_args):
        pytest.fail("invalid ST evidence reached reference I/O")

    dependencies = ReferencePreparationDependencies(
        lambda *_args: evidence,
        forbidden,
        forbidden,
        forbidden,
        lambda: False,
        _SOURCES,
    )
    with pytest.raises(ValueError, match="prepared_reference_incomplete"):
        PrepareReference(reference.official_universe, _NOW, dependencies)


@pytest.mark.parametrize("stage", ("st", "calendar"))
def test_cancellation_between_reference_steps_stops_further_io(stage):
    reference = PreparedFixture(_NOW)
    cancelled = False

    def st_evidence(*_args):
        nonlocal cancelled
        cancelled = stage == "st"
        return reference.st_evidence

    def calendar():
        nonlocal cancelled
        cancelled = True
        return reference.open_dates

    def forbidden(*_args):
        pytest.fail("cancelled preparation reached further I/O")

    dependencies = ReferencePreparationDependencies(
        st_evidence,
        calendar,
        forbidden,
        forbidden,
        lambda: cancelled,
        _SOURCES,
    )
    with pytest.raises(RuntimeError, match="reference_preparation_cancelled"):
        PrepareReference(reference.official_universe, _NOW, dependencies)


@pytest.mark.parametrize("industry_status", ("available", "empty", "failed"))
def test_industry_progress_explains_degradation_and_still_enters_market_update(industry_status):
    expected = PreparedFixture(_NOW)
    messages, published, downloaded = [], [], []

    def industry(_at, eligible, open_dates):
        assert messages[-1].task.label == "当前行业"
        assert eligible == expected.eligible_universe and open_dates == expected.open_dates
        assert "开始准备" in messages[-1].fields[0].text
        if industry_status == "failed":
            raise RuntimeError("private supplier payload must not appear in progress")
        return (_Industry(),) if industry_status == "available" else ()

    dependencies = ReferencePreparationDependencies(
        lambda *_args: expected.st_evidence,
        lambda: expected.open_dates,
        industry,
        lambda reference, _evidence: published.append(reference),
        lambda: False,
        _SOURCES,
    )

    def forbidden(_reference):
        pytest.fail("sync market must not run history or company")

    def market(reference):
        assert published == [reference]
        assert messages[-1].task.label == "参考发布"
        assert messages[-1].state == "完成"
        downloaded.append(reference)
        return QfqUpdateResult(_NOW.date(), 1)

    result = SyncData(
        lambda: PrepareReference(expected.official_universe, _NOW, dependencies, report=messages.append),
        forbidden,
        market,
        forbidden,
        lambda: False,
    ).execute("market")

    assert result.exit_code == 0 and len(downloaded) == 1
    industry_message = next(message.fields[0] for message in messages if message.task.label == "当前行业" and message.state in {"完成", "失败"})
    assert industry_message.level == (
        ProgressLevel.SUCCESS if industry_status == "available" else ProgressLevel.WARNING
    )
    if industry_status != "available":
        assert "继续资料同步" in industry_message.text
        assert published[0].industry_version is None
    assert "private supplier payload" not in str(messages)


def test_reference_completion_cut_preserves_the_actual_industry_received_time():
    expected = PreparedFixture(_NOW)
    received = _NOW + timedelta(seconds=8)
    dependencies = ReferencePreparationDependencies(
        lambda *_args: expected.st_evidence,
        lambda: expected.open_dates,
        lambda *_args: (_Industry(received_at=received),),
        lambda *_args: None,
        lambda: False,
        _SOURCES,
    )
    result = PrepareReference(expected.official_universe, _NOW, dependencies)
    assert result.checked_at == received
    assert result.st_evidence == expected.st_evidence


def test_partial_industry_preparation_reports_coverage_and_missing_count():
    first = PreparedFixture(_NOW).official_universe[0]
    expected = PreparedFixture(_NOW, (first, replace(first, code="600002")))
    messages = []
    dependencies = ReferencePreparationDependencies(
        lambda *_args: expected.st_evidence,
        lambda: expected.open_dates,
        lambda *_args: (_Industry(),),
        lambda *_args: None,
        lambda: False,
        _SOURCES,
    )
    PrepareReference(expected.official_universe, _NOW, dependencies, report=messages.append)
    industry_message = next(message for message in messages if message.task.label == "当前行业" and message.state == "完成")
    assert industry_message.fields[0].level is ProgressLevel.WARNING
    assert "1/2 股，缺分类 1 股" in industry_message.fields[0].text


def test_reference_logs_use_injected_sources_and_keep_publication_local():
    expected = PreparedFixture(_NOW)
    messages = []
    dependencies = ReferencePreparationDependencies(
        lambda *_args: expected.st_evidence,
        lambda: expected.open_dates,
        lambda *_args: (_Industry(),),
        lambda *_args: None,
        lambda: False,
        _SOURCES,
    )
    PrepareReference(expected.official_universe, _NOW, dependencies, report=messages.append)

    for message in messages:
        expected_source = {"ST资格": _SOURCES.st, "交易日历": _SOURCES.calendar, "当前行业": _SOURCES.industry, "参考发布": "本地"}[message.task.label]
        assert message.fields[-1].text == f"来源 {expected_source}"
    assert "Tencent" not in str(messages) and "BaoStock" not in str(messages)


@pytest.mark.parametrize("sources", (("", "calendar", "industry"), ("st", " ", "industry"), ("st", "calendar", "")))
def test_reference_source_labels_cannot_be_missing(sources):
    with pytest.raises(ValueError, match="reference_preparation_source_invalid"):
        ReferencePreparationSources(*sources)

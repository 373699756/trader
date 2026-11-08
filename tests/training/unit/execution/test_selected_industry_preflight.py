from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tests.helpers.download.history.qfq_windows import _cell
from tests.helpers.download.published_history import publish_history_fixture
from tests.helpers.shared.configuration import SqlitePolicyFixture
from tests.helpers.training.current_industry import CurrentIndustrySnapshotFixture
from tests.helpers.training.profiles.v1.learned_rank_training import _settings
from tests.helpers.training.training_resources import TrainingResourceSettingsFixture
from trader.download.domain.baostock_daily import (
    BaoStockCalendar,
    BaoStockIndustryInterval,
    BaoStockSecurity,
    BaoStockSourceVersions,
)
from trader.download.domain.history_reference import HistoryStEvidence
from trader.download.domain.history_sync import HistorySupplierContext
from trader.infra.settings import LoadRuntimeSettings
from trader.training.application.learned_rank_training import V1TrainingResult
from trader.training.application.selected_training import SelectedTrainingTask, TrainSelectedProfiles, TrainingLocations
from trader.training.infra import selected_training
from trader.training.infra.cpu_admission import TrainingCpuBudget
from trader.training.infra.execution_timing import TrainingExecutionMeasurements
from trader.training.infra.history.history_training_input import SQLiteHistoryTrainingInputSnapshot
from trader.training.infra.memory_admission import TrainingMemoryBudget
from trader.training.infra.selected_training import SelectedTrainingAdapter, SelectedTrainingRequest

_ROOT = Path(__file__).resolve().parents[4]


_CURRENT_INDUSTRY = CurrentIndustrySnapshotFixture(("000001", *(f"60000{number}" for number in range(6))))


class _Progress:
    def publish(self, _update):
        pass


def _request(tmp_path: Path, *, industry: bool) -> SelectedTrainingRequest:
    days = tuple(date(2024, 1, 1) + timedelta(days=position) for position in range(340))
    security = BaoStockSecurity("000001", "样本银行", "main", date(1991, 4, 3), None, "fixture")
    intervals = (
        (BaoStockIndustryInterval(
            security.code, days[-15], days[-14], "capco:J66货币金融服务", "中国上市公司协会上市公司行业分类标准",
        ),) if industry else ()
    )
    context = HistorySupplierContext(
        BaoStockCalendar(days),
        (security,),
        BaoStockSourceVersions("fixture", "3.11", ()),
        intervals,
        (HistoryStEvidence(security.code, days[-1], "clear"),),
    )
    root = tmp_path / "history"
    publish_history_fixture(
        root, context, (_cell(security.code, days[-1]),),
        datetime.combine(days[-1], datetime.min.time(), ZoneInfo("Asia/Shanghai")),
    )
    resources = TrainingResourceSettingsFixture()
    return SelectedTrainingRequest(
        LoadRuntimeSettings(_ROOT / "config/runtime.json"),
        ("v1",),
        (_Progress(),),
        lambda: False,
        TrainingMemoryBudget(1024**2),
        TrainingLocations(root, tmp_path / "models", tmp_path / "samples"),
        cpu_budget=TrainingCpuBudget(2, settings=resources.cpu),
        fanout_settings=resources.fanout,
        sample_settings=resources.samples,
    )


def test_missing_industry_rejects_v1_before_reading_prices_or_fitting(tmp_path, monkeypatch):
    request = _request(tmp_path, industry=False)
    monkeypatch.setattr(selected_training, "LoadV1TrainingSettings", lambda _root: _settings())
    monkeypatch.setattr(
        SQLiteHistoryTrainingInputSnapshot, "iter_training_rows",
        lambda *_args, **_kwargs: pytest.fail("missing industry must not start a price scan"),
    )
    monkeypatch.setattr(
        selected_training, "RunV1Training",
        lambda *_args, **_kwargs: pytest.fail("missing industry must not invoke fitting"),
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = TrainSelectedProfiles(SelectedTrainingAdapter(request, sqlite_policy=SqlitePolicyFixture(), load_industry_snapshot=lambda _observed: CurrentIndustrySnapshotFixture(()), clock=lambda: _CURRENT_INDUSTRY.observed_at), executor)
    assert result.history_scan_passes == result.history_rows_read == 0
    assert result.outcomes[0].failure is None
    assert result.outcomes[0].result.status == "blocked"
    assert all(item.effective_dates == 0 for item in result.outcomes[0].result.date_coverage)


def test_current_industry_applies_to_all_signal_dates_regardless_of_old_interval(tmp_path, monkeypatch):
    request = _request(tmp_path, industry=True)
    monkeypatch.setattr(selected_training, "LoadV1TrainingSettings", lambda _root: _settings())
    source = SQLiteHistoryTrainingInputSnapshot.open(request.paths.history, industry_snapshot=_CURRENT_INDUSTRY, sqlite_policy=SqlitePolicyFixture())
    assert source.has_industry_window(frozenset(source.snapshot.calendar.open_dates), 1)
    assert source.has_industry_window(frozenset(source.snapshot.calendar.open_dates), 2)
    monkeypatch.setattr(
        selected_training, "RunV1Training",
        lambda *_args, **_kwargs: pytest.fail("preparation must not run a fit before worker execution"),
    )
    with ExitStack() as resources:
        factory = selected_training._TrainingTaskFactory(
            request, source, resources, TrainingExecutionMeasurements(("v1",), lambda: 0),
            sqlite_policy=SqlitePolicyFixture(),
        )
        task = factory.prepare("v1", request.progresses[0])
    assert task.profile == "v1" and task.readable_dates
    assert task.readable_dates.issubset(source.snapshot.calendar.open_dates)


def test_selected_profiles_freeze_one_clock_and_current_industry_load_for_the_run(tmp_path, monkeypatch):
    request = _request(tmp_path, industry=False)
    request = replace(request, profiles=("v1", "v2", "v3", "v5"), progresses=tuple(_Progress() for _ in range(4)))
    observed, loaded, adopted = [], [], []

    def clock():
        observed.append(_CURRENT_INDUSTRY.observed_at)
        return observed[-1]

    def load(now):
        loaded.append(now)
        return _CURRENT_INDUSTRY

    def prepare(factory, profile, _progress):
        adopted.append(factory.history.snapshot.industry_snapshot)
        return SelectedTrainingTask(profile, frozenset(), lambda _rows: None)

    monkeypatch.setattr(selected_training._TrainingTaskFactory, "prepare", prepare)
    adapter = SelectedTrainingAdapter(request, sqlite_policy=SqlitePolicyFixture(), load_industry_snapshot=load, clock=clock)
    with adapter.open() as session:
        for profile, progress in zip(request.profiles, request.progresses, strict=True):
            assert session.prepare_task(profile, progress).profile == profile
    assert observed == loaded == [_CURRENT_INDUSTRY.observed_at]
    assert len(adopted) == 4 and all(snapshot is adopted[0] for snapshot in adopted)
    assert tuple(member.code for member in adopted[0].members) == ("000001",)


def test_reusable_v1_model_skips_industry_preflight_and_price_scan(tmp_path, monkeypatch):
    request = _request(tmp_path, industry=False)
    reused = V1TrainingResult("engineering_ready", "fixture", "a" * 64, 0, reused=True)
    monkeypatch.setattr(selected_training, "LoadV1TrainingSettings", lambda _root: _settings())
    monkeypatch.setattr(selected_training, "InspectV1Training", lambda *_args, **_kwargs: reused)
    monkeypatch.setattr(
        SQLiteHistoryTrainingInputSnapshot, "has_industry_window",
        lambda *_args, **_kwargs: pytest.fail("a complete model reuse must not require new industry input"),
    )
    monkeypatch.setattr(
        SQLiteHistoryTrainingInputSnapshot, "iter_training_rows",
        lambda *_args, **_kwargs: pytest.fail("model reuse must not start a price scan"),
    )
    monkeypatch.setattr(
        selected_training, "RunV1Training",
        lambda *_args, **_kwargs: pytest.fail("model reuse must not invoke fitting"),
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = TrainSelectedProfiles(SelectedTrainingAdapter(request, sqlite_policy=SqlitePolicyFixture(), load_industry_snapshot=lambda _observed: _CURRENT_INDUSTRY, clock=lambda: _CURRENT_INDUSTRY.observed_at), executor)
    assert result.history_scan_passes == result.history_rows_read == 0
    assert result.outcomes[0].failure is None and result.outcomes[0].result is reused

"""Metadata proves impossibility without opening a price stream; exact checks stay separate."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tests.helpers.download.history.qfq_windows import _cell
from tests.helpers.download.published_history import publish_history_fixture
from tests.helpers.shared.configuration import SqlitePolicyFixture
from tests.helpers.training.current_industry import CurrentIndustrySnapshotFixture
from tests.helpers.training.training_resources import TrainingResourceSettingsFixture
from trader.download.domain.baostock_daily import BaoStockCalendar, BaoStockSecurity, BaoStockSourceVersions
from trader.download.domain.history_reference import HistoryStEvidence
from trader.download.domain.history_sync import HistorySupplierContext
from trader.infra.settings import LoadRuntimeSettings
from trader.recommendation.domain.publication.models import Strategy
from trader.training.application.selected_training import TrainSelectedProfiles, TrainingLocations
from trader.training.domain.training_date_coverage import PossibleTrainingSignalDates, TrainingDateCoverage
from trader.training.infra.cpu_admission import TrainingCpuBudget
from trader.training.infra.history.history_training_input import SQLiteHistoryTrainingInputSnapshot
from trader.training.infra.memory_admission import TrainingMemoryBudget
from trader.training.infra.selected_training import SelectedTrainingAdapter, SelectedTrainingRequest


@pytest.mark.parametrize(("history", "excludes", "first"), ((251, True, 251), (251, False, 250), (61, False, 60), (61, True, 61)))
@pytest.mark.parametrize("maturity", (1, 5))
def test_signal_upper_bound_uses_market_sessions_and_profile_feature_anchor(history, excludes, first, maturity):
    calendar = tuple(date(2020, 1, 1) + timedelta(days=position * 3) for position in range(340))
    listed, delisted = calendar[3], calendar[-2]
    possible = PossibleTrainingSignalDates(
        calendar, calendar[-1], ("600001",), (("600001", listed, delisted),),
        history_sessions=history, history_excludes_signal=excludes, maturity_sessions=maturity,
    )
    assert possible == calendar[3 + first:len(calendar) - 2 - maturity]
    assert calendar[-2 - maturity] not in possible  # The target on the delisting day is unavailable.


def test_global_upper_bound_is_a_union_not_a_per_stock_minimum_and_unknown_listing_cannot_disprove_dates():
    calendar = tuple(date(2020, 1, 1) + timedelta(days=position) for position in range(400))
    parameters = dict(history_sessions=61, history_excludes_signal=True, maturity_sessions=5)
    both = PossibleTrainingSignalDates(
        calendar, calendar[-1], ("600001", "600002"),
        (("600001", calendar[0], None), ("600002", calendar[-2], None)), **parameters,
    )
    assert both == calendar[61:-5]
    assert PossibleTrainingSignalDates(calendar, calendar[-1], ("600003",), (), **parameters) == both
    assert PossibleTrainingSignalDates(calendar, calendar[-1], (), (), **parameters) == ()
    separated = PossibleTrainingSignalDates(
        calendar, calendar[-1], ("600001", "600002"),
        (("600001", calendar[0], calendar[100]), ("600002", calendar[200], None)), **parameters,
    )
    assert separated == (*calendar[61:95], *calendar[261:-5])


def test_coverage_preserves_complete_missing_dates_and_distinguishes_evidence_basis():
    days = tuple(date(2020, 1, 1) + timedelta(days=position) for position in range(5))
    observed = TrainingDateCoverage(Strategy.D25, "final_fitting", "sample_index", days[-1] + timedelta(days=5), days[-1], days, days[1:3])
    assert observed.expected_dates == 5 and observed.effective_dates == 2
    assert observed.missing_dates == (days[0], *days[3:])
    with pytest.raises(ValueError, match="training_date_coverage_invalid"):
        TrainingDateCoverage(Strategy.D25, "final_fitting", "sample_index", days[-1] + timedelta(days=5), days[-1], days[1:], days)


class _Progress:
    def publish(self, _update):
        pass


def test_all_four_profiles_reject_impossible_listing_dates_before_shared_price_scan(tmp_path, monkeypatch):
    days = tuple(date(2018, 1, 1) + timedelta(days=position) for position in range(2000))
    security = BaoStockSecurity("600001", "样本银行", "main", days[-2], None, "fixture")
    context = HistorySupplierContext(
        BaoStockCalendar(days), (security,), BaoStockSourceVersions("fixture", "3.11", ()), (),
        (HistoryStEvidence(security.code, days[-1], "clear"),),
    )
    history_root = tmp_path / "history"
    publish_history_fixture(
        history_root, context, (_cell(security.code, days[-1]),),
        datetime.combine(days[-1], datetime.min.time(), ZoneInfo("Asia/Shanghai")),
    )
    current = CurrentIndustrySnapshotFixture((security.code,))
    resources = TrainingResourceSettingsFixture()
    request = SelectedTrainingRequest(
        LoadRuntimeSettings(Path(__file__).resolve().parents[4] / "config/runtime.json"),
        ("v1", "v2", "v3", "v5"), tuple(_Progress() for _ in range(4)), lambda: False,
        TrainingMemoryBudget(1024**2), TrainingLocations(history_root, tmp_path / "models", tmp_path / "samples"),
        cpu_budget=TrainingCpuBudget(2, settings=resources.cpu), fanout_settings=resources.fanout,
        sample_settings=resources.samples,
    )
    monkeypatch.setattr(SQLiteHistoryTrainingInputSnapshot, "iter_training_rows", lambda *_a, **_kw: pytest.fail("preflight opened prices"))
    with ThreadPoolExecutor(max_workers=4) as executor:
        run = TrainSelectedProfiles(SelectedTrainingAdapter(
            request, sqlite_policy=SqlitePolicyFixture(), load_industry_snapshot=lambda _now: current,
            clock=lambda: current.observed_at,
        ), executor)
    assert run.history_scan_passes == run.history_rows_read == 0
    assert all(item.failure is None and item.result.status == "blocked" for item in run.outcomes)
    for outcome in run.outcomes:
        observations = outcome.result.date_coverage if outcome.profile == "v1" else tuple(item for head in outcome.result.heads for item in head.date_coverage)
        assert {item.strategy for item in observations} == {Strategy.TOMORROW, Strategy.D25}
        assert all(item.basis == "metadata_upper_bound" and item.effective_dates == 0 for item in observations)
        assert all(item.missing_dates == item.required_dates for item in observations)

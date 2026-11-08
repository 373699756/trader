"""Expected maintenance contracts use the same inputs as actual fitting requests."""

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tests.helpers.training.current_industry import CurrentIndustrySnapshotFixture
from trader.training.application.resources import TrainingProfileThreads
from trader.training.infra.history.history_training_expectation import HistoryTrainingExpectationReader
from trader.training.infra.profile.v2.contracts import V2_TRAINING_PROFILE
from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE
from trader.training.infra.profile.v5.contracts import V5_TRAINING_PROFILE
from trader.training.infra.profile.v5.settings import TcnFittingParameters
from trader.training.infra.training_contract import BuildTrainingContractHash
from trader.training.infra.training_settings import LoadProfileTrainingSettings

_ROOT = Path(__file__).resolve().parents[5]
_NOW = datetime(2026, 11, 8, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.mark.parametrize("profile", (V2_TRAINING_PROFILE, V3_TRAINING_PROFILE, V5_TRAINING_PROFILE))
def test_expected_contracts_keep_real_settings_and_profile_threads(profile):
    settings = LoadProfileTrainingSettings(_ROOT, profile.profile_id)
    threads = TrainingProfileThreads(1, 3, 4)
    industry = CurrentIndustrySnapshotFixture(("600001",), observed_at=_NOW)
    reader = HistoryTrainingExpectationReader(lambda _at: industry, lambda _profile: settings, lambda: threads)
    result = reader.read(profile, _NOW)
    expected_threads = settings.fitting.compute_threads if isinstance(settings.fitting, TcnFittingParameters) else threads.for_profile(profile.profile_id)
    assert result.industry_snapshot is industry
    assert result.contracts == tuple(
        (head.strategy, BuildTrainingContractHash(profile, head, settings, compute_threads=expected_threads))
        for head in profile.heads
    )
    changed = replace(settings, fit_dates=settings.fit_dates + 1)
    changed_reader = replace(reader, load_settings=lambda _profile: changed)
    assert changed_reader.read(profile, _NOW).contracts != result.contracts


@pytest.mark.parametrize("offset", (-1, 7 * 24 * 60 * 60))
def test_expected_inputs_do_not_reuse_future_or_expired_industry(offset):
    industry = CurrentIndustrySnapshotFixture(("600001",), observed_at=_NOW)
    settings = LoadProfileTrainingSettings(_ROOT, "v3")
    reader = HistoryTrainingExpectationReader(lambda _at: industry, lambda _profile: settings, lambda: TrainingProfileThreads(1, 2, 3))
    with pytest.raises(ValueError, match="current_industry_snapshot_expired"):
        reader.read(V3_TRAINING_PROFILE, _NOW + timedelta(seconds=offset))

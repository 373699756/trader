"""Publication observation reads declared metadata identities, never model payloads."""

import json
from dataclasses import replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.helpers.training.configuration import V1TrainingSettingsFixture
from tests.helpers.training.current_industry import CurrentIndustrySnapshotFixture
from tests.helpers.training.profiles.v3.profile import _publish_profile
from tests.helpers.shared.configuration import SqlitePolicyFixture
from tests.helpers.training.history_training_due import HistoryTrainingExpectationFixture
from trader.download.domain.baostock_daily import BaoStockCalendar
from trader.infra.regular_file import ReadRegularFile
from trader.infra.serialization.canonical import ContentHash
from trader.recommendation.domain.market.v1.features import LEARNED_RANK_FEATURE_IDS
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.infra.scoring.profile_factory import LoadScoringProfile
from trader.training.domain.final_fit import BuildFinalFitDatePlan
from trader.training.domain.training_input_identity import BuildTrainingInputHash
from trader.training.domain.v1.learned_rank import (
    BuildLearnedRankDateSplit,
    LearnedRankContractHash,
    LearnedRankFinalContractHash,
    LearnedRankFinalHead,
    LearnedRankFinalPair,
    LearnedRankHead,
    LearnedRankPredictionMetrics,
)
from trader.training.infra.model_bundles import bundle_snapshot
from trader.training.infra.model_bundles.bundle_codec import ModelBundleContractError, ModelBundleIntegrityError
from trader.training.infra.model_bundles.profile_publication import (
    ProfilePublicationBusyError,
    ProfilePublicationLock,
    ReadPublishedProfileIdentities,
)
from trader.training.infra.profile.v1 import models as v1_models
from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE
from trader.training.infra.history.history_training_due import PublishedTrainingState


def test_standard_observation_matches_loaded_identity_without_reading_model_payload(tmp_path, monkeypatch):
    _publish_profile(tmp_path, V3_TRAINING_PROFILE)
    expected = LoadScoringProfile("v3", training_root=tmp_path).publications
    reads = []

    def metadata_only(path, *, maximum_bytes):
        assert path.name == "metadata.json" and maximum_bytes == 8 * 1024 * 1024
        reads.append(path)
        return ReadRegularFile(path, maximum_bytes=maximum_bytes)

    monkeypatch.setattr(bundle_snapshot, "ReadRegularFile", metadata_only)
    monkeypatch.setattr(bundle_snapshot, "LoadHeadBundle", lambda *args: pytest.fail("no model payload loading"))
    # Its declared metadata identity is observable even if payload validation
    # would fail; observation never signs model integrity or production readiness.
    (tmp_path / "v3/tomorrow/model.json").write_text("{", encoding="utf-8")
    actual = ReadPublishedProfileIdentities(
        tmp_path / "v3", V3_TRAINING_PROFILE, cancel_requested=lambda: False,
    )
    assert actual == expected
    assert reads == [tmp_path / "v3/tomorrow/metadata.json", tmp_path / "v3/d25/metadata.json"]


@pytest.mark.parametrize("resign", [False, True])
def test_standard_observation_rejects_tampered_or_resigned_wrong_contract(tmp_path, resign):
    _publish_profile(tmp_path, V3_TRAINING_PROFILE)
    path = tmp_path / "v3/tomorrow/metadata.json"
    metadata = json.loads(path.read_text(encoding="utf-8"))
    metadata["model_id"] = "wrong-head"
    if resign:
        metadata.pop("content_hash")
        metadata["content_hash"] = ContentHash(metadata)
    path.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(ModelBundleContractError if resign else ModelBundleIntegrityError):
        ReadPublishedProfileIdentities(tmp_path / "v3", V3_TRAINING_PROFILE, cancel_requested=lambda: False)


@pytest.mark.parametrize("unsafe", ["size", "symlink"])
def test_observation_rejects_nonregular_or_oversized_metadata_before_decoding(tmp_path, unsafe):
    _publish_profile(tmp_path, V3_TRAINING_PROFILE)
    path = tmp_path / "v3/tomorrow/metadata.json"
    if unsafe == "size":
        with path.open("wb") as stream:
            stream.seek(8 * 1024 * 1024)
            stream.write(b" ")
    else:
        target = tmp_path / "saved-metadata.json"
        path.rename(target)
        path.symlink_to(target)
    with pytest.raises(ValueError, match="regular_file"):
        ReadPublishedProfileIdentities(tmp_path / "v3", V3_TRAINING_PROFILE, cancel_requested=lambda: False)


def test_publication_observation_lock_wait_is_bounded_and_cancellable(tmp_path):
    _publish_profile(tmp_path, V3_TRAINING_PROFILE)
    profile_root = tmp_path / "v3"
    with ProfilePublicationLock(profile_root):
        with pytest.raises(ProfilePublicationBusyError):
            ReadPublishedProfileIdentities(
                profile_root, V3_TRAINING_PROFILE, cancel_requested=lambda: False, timeout_seconds=0.01,
            )
        checks = 0

        def cancelled():
            nonlocal checks
            checks += 1
            return checks >= 3

        with pytest.raises(InterruptedError, match="observation_cancelled"):
            ReadPublishedProfileIdentities(
                profile_root, V3_TRAINING_PROFILE, cancel_requested=cancelled,
            )
    assert checks == 3


def _v1_pair() -> LearnedRankFinalPair:
    calendar = tuple(date(2024, 1, 1) + timedelta(days=index) for index in range(300))
    settings = V1TrainingSettingsFixture(
        fit_dates=2, early_stopping_dates=1, calibration_dates=1, validation_dates=1,
    )
    industry = CurrentIndustrySnapshotFixture(("600001",))
    width = len(LEARNED_RANK_FEATURE_IDS)
    metrics = LearnedRankPredictionMetrics(1, 1, 1, 1, 1, 0.1, 0.1, 0.0, 0.5)

    def head(strategy):
        maturity = 1 if strategy is Strategy.TOMORROW else 5
        split = BuildLearnedRankDateSplit(calendar, settings, maturity_sessions=maturity)
        diagnostic = LearnedRankHead(
            strategy,
            LearnedRankContractHash(settings),
            split,
            (0.0,) * width,
            (1.0,) * width,
            (0.0,) * width,
            0.0,
            0.0,
            1.0,
            "fixture-native-model-never-instantiated",
            1,
            1,
            1,
            1,
            (industry.members[0].reference.industry_id,),
            metrics,
        )
        return LearnedRankFinalHead(
            strategy,
            LearnedRankFinalContractHash(settings),
            split,
            diagnostic.transformer_means,
            diagnostic.transformer_scales,
            diagnostic.ridge_coefficients,
            diagnostic.ridge_intercept,
            diagnostic.calibration_intercept,
            diagnostic.calibration_slope,
            diagnostic.rank_model,
            diagnostic.rank_iterations,
            diagnostic.training_rows,
            diagnostic.early_stopping_rows,
            diagnostic.calibration_rows,
            diagnostic.industries,
            diagnostic.validation,
            BuildFinalFitDatePlan(
                calendar, source_cutoff=calendar[-1], fit_sessions=settings.fit_dates, maturity_sessions=maturity,
            ),
            diagnostic,
        )

    heads = (head(Strategy.TOMORROW), head(Strategy.D25))
    arguments = (
        BuildTrainingInputHash("a" * 64, industry.content_hash),
        "b" * 64,
        BaoStockCalendar(calendar).content_hash,
        calendar[-1],
        settings,
        heads,
        "c" * 64,
    )
    return LearnedRankFinalPair(*arguments, calendar, price_snapshot_hash="a" * 64, industry_snapshot=industry)


def test_v1_metadata_observation_keeps_its_actual_loaded_identity_without_native_models(tmp_path, monkeypatch):
    monkeypatch.setattr(v1_models, "LearnedRankPredictor", lambda head: None)
    v1_models.PublishLearnedRankPair(tmp_path, _v1_pair())
    expected = v1_models.LoadV1ScoringProfile(tmp_path).publications
    reads = []

    def metadata_only(path, *, maximum_bytes):
        assert path.name == "metadata.json" and maximum_bytes == 8 * 1024 * 1024
        reads.append(path)
        return ReadRegularFile(path, maximum_bytes=maximum_bytes)

    monkeypatch.setattr(v1_models, "ReadRegularFile", metadata_only)
    monkeypatch.setattr(v1_models, "ReadLearnedRankPair", lambda *args: pytest.fail("no V1 model pair loading"))
    monkeypatch.setattr(v1_models, "LearnedRankPredictor", lambda head: pytest.fail("no V1 native predictors"))
    actual = v1_models.ReadV1PublicationIdentities(tmp_path, cancel_requested=lambda: False)
    assert actual == expected
    assert reads == [tmp_path / "v1/tomorrow/metadata.json", tmp_path / "v1/d25/metadata.json"]


def test_v1_observation_rejects_self_hash_conflicts_and_pre_cancelled_read(tmp_path, monkeypatch):
    monkeypatch.setattr(v1_models, "LearnedRankPredictor", lambda head: None)
    v1_models.PublishLearnedRankPair(tmp_path, _v1_pair())
    path = tmp_path / "v1/tomorrow/metadata.json"
    metadata = json.loads(path.read_text(encoding="utf-8"))
    metadata["head_content_hash"] = "f" * 64
    path.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(ModelBundleIntegrityError):
        v1_models.ReadV1PublicationIdentities(tmp_path, cancel_requested=lambda: False)
    monkeypatch.setattr(v1_models, "ReadRegularFile", lambda *args, **kwargs: pytest.fail("no cancelled read"))
    with pytest.raises(InterruptedError):
        v1_models.ReadV1PublicationIdentities(tmp_path, cancel_requested=lambda: True)


@pytest.mark.parametrize("initial_busy", [False, True])
def test_model_observation_updates_restart_hint_and_preserves_loaded_identity_on_busy(tmp_path, monkeypatch, initial_busy):
    _publish_profile(tmp_path, V3_TRAINING_PROFILE)
    loaded = LoadScoringProfile("v3", training_root=tmp_path).publications
    state = PublishedTrainingState(
        tmp_path / "history", tmp_path, loaded, owned_profile="v3",
        maintenance_path=tmp_path / "maintenance.sqlite3",
        load_expectation=HistoryTrainingExpectationFixture,
        wall_clock=lambda: datetime(2026, 11, 8, tzinfo=ZoneInfo("Asia/Shanghai")),
        sqlite_policy=SqlitePolicyFixture(),
    )
    if initial_busy:
        with ProfilePublicationLock(tmp_path / "v3"):
            state.initialize()
        assert all(item.disk is None and item.failure == "publication_busy"
                   for item in state.status() if item.profile_id == "v3")
    else:
        state.initialize()
    assert not any(item.restart_required for item in state.status())
    changed = tuple(replace(item, metadata_hash="f" * 64) for item in loaded)

    def observe(root, profile, **kwargs):
        return changed if profile.profile_id == "v3" else ()

    monkeypatch.setattr("trader.training.infra.history.history_training_due.ReadPublishedProfileIdentities", observe)
    state.observe_publication(lambda: False)
    heads = tuple(item for item in state.status() if item.profile_id == "v3")
    assert tuple(item.loaded for item in heads) == loaded
    assert tuple(item.disk for item in heads) == changed
    assert all(item.restart_required for item in heads)

    def busy(*args, **kwargs):
        raise ProfilePublicationBusyError("publication busy")

    monkeypatch.setattr("trader.training.infra.history.history_training_due.ReadPublishedProfileIdentities", busy)
    state.observe_publication(lambda: False)
    heads = tuple(item for item in state.status() if item.profile_id == "v3")
    assert tuple(item.disk for item in heads) == changed
    assert all(item.restart_required and item.publication_observation_failure for item in heads)
    state.observe_publication(lambda: True)
    assert tuple(item for item in state.status() if item.profile_id == "v3") == heads

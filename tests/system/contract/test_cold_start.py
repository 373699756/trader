from __future__ import annotations


import json
import threading

from datetime import datetime


from zoneinfo import ZoneInfo

import pytest

from tests.helpers.recommendation.publication.freeze_records import record

from tests.helpers.training.profiles.v3.profile import _publish_profile, _write_bundle

from trader.bootstrap import BuildProfiledSystem, BuildSystem

from trader.recommendation.application.pipeline.local_score.model_router import ModelScoringRouter

from trader.recommendation.application.pipeline.local_score.unavailable_model import UnavailableModelCapability

from trader.recommendation.domain.publication.models import Strategy

from trader.recommendation.domain.scoring.profile_identity import SCORING_PROFILE_IDS

from trader.training.infra.model_bundles.profile_publication import InspectPublishedProfileBundles
from trader.training.infra.model_bundles.contracts import D25ProfileContract

from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE

from tests.helpers.shared.cold_start import (
    _empty_config,
)


@pytest.mark.parametrize(
    "profiles",
    (*((profile,) for profile in SCORING_PROFILE_IDS), ("v1", "v5"), SCORING_PROFILE_IDS),
    ids=lambda profiles: ",".join(profiles),
)
@pytest.mark.parametrize("hour,minute", ((8, 0), (10, 0), (12, 0), (14, 59), (15, 10), (20, 30)))
def test_empty_clone_keeps_web_readable_and_models_inactive(tmp_path, monkeypatch, caplog, profiles, hour, minute):
    config = _empty_config(tmp_path)
    now = datetime(2026, 10, 9, hour, minute, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr("trader.bootstrap._utc_now", lambda: now)
    started = []
    monkeypatch.setattr(threading.Thread, "start", lambda thread: started.append(thread.name))

    system = BuildProfiledSystem(config, profiles=profiles)

    client = system.app.test_client()
    assert client.get("/").status_code == 200
    for selected in profiles:
        payload = client.get(f"/api/status?profile={selected}").get_json()
        scoring = payload["scoring_profile"]
        assert scoring["profile_id"] == selected
        assert set(scoring["heads"]) == {"tomorrow", "d25"}
        for strategy, head in scoring["heads"].items():
            assert head["is_enable"] is False
            assert head["production_authority"] is True
            assert head["model_hash"] == ""
            reason = "tomorrow_probability_model_unavailable" if strategy == "tomorrow" else "production_model_missing"
            assert head["historical_failure_reasons"] == [reason]
        assert "./run.sh train" in caplog.text
    assert "./run.sh sync history" in caplog.text
    assert "缺少已发布完整历史" in caplog.text
    assert "缺少行情短窗口" not in caplog.text
    assert started == []
    assert not (tmp_path / "data").exists()
    assert not (tmp_path / ".runtime").exists()


def test_missing_profile_does_not_disable_a_complete_other_profile(tmp_path, monkeypatch, caplog):
    config = _empty_config(tmp_path)
    _publish_profile(tmp_path / "data/training/models", D25ProfileContract(V3_TRAINING_PROFILE))
    seen = []

    def load_target(model_root, profile, target):
        seen.append((profile.profile_id, target.threshold_bps))
        return profile

    monkeypatch.setattr("trader.training.infra.d25_probability.LoadD25ScoringProfile", load_target)
    system = BuildProfiledSystem(config)
    client = system.app.test_client()

    for profile, is_enabled in (("v2", False), ("v3", True)):
        scoring = client.get(f"/api/status?profile={profile}").get_json()["scoring_profile"]
        assert scoring["heads"]["d25"]["is_enable"] is is_enabled
        assert not scoring["heads"]["tomorrow"]["is_enable"]
    assert "V2 双头模型工件缺失" in caplog.text
    assert "V3 双头模型工件缺失" not in caplog.text
    assert seen == [("v3", 100)]


def test_all_tomorrow_profiles_load_independently_with_equal_targets(tmp_path, monkeypatch):
    class Predictor:
        def __init__(self, identity):
            self.identity = identity

        def predict_probabilities(self, features):
            return (0.25,) * len(features)

    calls = []

    def load(model_root, profile, target, *, project_root):
        calls.append((profile, target.threshold_bps))
        return Predictor(profile[-1] * 64)

    monkeypatch.setattr("trader.training.infra.tomorrow_probability.LoadTomorrowProbabilityModel", load)
    system = BuildProfiledSystem(_empty_config(tmp_path))
    client = system.app.test_client()
    assert sorted(calls) == [(profile, 500) for profile in SCORING_PROFILE_IDS]
    for profile in SCORING_PROFILE_IDS:
        heads = client.get(f"/api/status?profile={profile}").get_json()["scoring_profile"]["heads"]
        assert heads["tomorrow"]["is_enable"]
        assert heads["tomorrow"]["model_hash"] == profile[-1] * 64
        assert not heads["d25"]["is_enable"]
    assert all(lane.scheduler._dependencies.scored_strategies == (Strategy.TOMORROW,) for lane in system.systems)


def test_changing_one_tomorrow_target_does_not_disable_other_profiles(tmp_path, monkeypatch):
    config = _empty_config(tmp_path)
    path = config.with_name("selection.json")
    document = json.loads(path.read_text())
    document["prediction_targets"]["v1"]["tomorrow_bps"] = 600
    path.write_text(json.dumps(document))
    loaded = []

    class Predictor:
        identity = "a" * 64

    def load(model_root, profile, target, *, project_root):
        loaded.append((profile, target.threshold_bps))
        if profile == "v1":
            raise FileNotFoundError
        return Predictor()

    monkeypatch.setattr("trader.training.infra.tomorrow_probability.LoadTomorrowProbabilityModel", load)
    system = BuildProfiledSystem(config, profiles=("v1", "v5", "v3", "v2"))
    assert sorted(loaded) == [("v1", 600), ("v2", 500), ("v3", 500), ("v5", 500)]
    client = system.app.test_client()
    for profile in ("v1", "v2", "v3", "v5"):
        payload = client.get(f"/api/status?profile={profile}").get_json()
        assert payload["scoring_profile"]["heads"]["tomorrow"]["is_enable"] is (profile != "v1")


def test_partial_or_corrupt_profile_keeps_web_readable_with_classified_model_failure(tmp_path, monkeypatch):
    now = datetime(2026, 10, 20, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr("trader.bootstrap._utc_now", lambda: now)
    config = _empty_config(tmp_path)
    contract = V3_TRAINING_PROFILE.heads[0]
    _write_bundle(tmp_path / "data/training/models/v3" / contract.directory_name, contract)
    system = BuildSystem(config, scoring_profile="v3")
    scoring = system.app.test_client().get("/api/status").get_json()["scoring_profile"]
    assert all(head["is_enable"] is False for head in scoring["heads"].values())
    original = record()
    system.records.initialize()
    system.records.commit(original)
    route = f"/api/freezes/tomorrow/history?date={original.trade_date.isoformat()}"
    before = system.app.test_client().get(route).get_json()

    _publish_profile(tmp_path / "data/training/models", V3_TRAINING_PROFILE)
    metadata = (
        InspectPublishedProfileBundles(tmp_path / "data/training/models/v3", V3_TRAINING_PROFILE)[0].model_path.parent
        / "metadata.json"
    )
    metadata.write_text("{}", encoding="utf-8")
    degraded = BuildSystem(config, scoring_profile="v3")
    client = degraded.app.test_client()
    assert client.get("/").status_code == 200
    scoring = client.get("/api/status").get_json()["scoring_profile"]
    assert scoring["profile_id"] == "v3"
    for head in scoring["heads"].values():
        assert head["is_enable"] is False
        assert head["historical_failure_reasons"] == ["production_model_integrity_conflict"]
    after = client.get(route).get_json()
    assert after == before
    assert after["items"][0]["code"] == original.freeze.items[0].code


@pytest.mark.parametrize("profile,sessions", (("v2", 251), ("v3", 61)))
def test_unavailable_model_cannot_admit_or_fabricate_predictions(application_feature_factory, profile, sessions):
    capability = UnavailableModelCapability(profile, sessions)
    router = ModelScoringRouter(profile, {strategy: capability for strategy in (Strategy.TOMORROW, Strategy.D25)})
    feature = application_feature_factory("600001", datetime(2026, 10, 9, 10, tzinfo=ZoneInfo("Asia/Shanghai")))
    for strategy in (Strategy.TOMORROW, Strategy.D25):
        assert router.uses_model(strategy) is True
        assert router.history_required_sessions(strategy) == sessions
        assert router.is_input_eligible(strategy, feature) is False
        with pytest.raises(RuntimeError, match="production_model_missing"):
            router.score(strategy, (feature,))

from __future__ import annotations

import shutil
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tests.unit.infra.test_decision_records import record
from tests.unit.training.infra.profile.v3.test_profile import _publish_profile, _write_bundle
from trader.bootstrap import BuildProfiledSystem, BuildSystem
from trader.recommendation.application.pipeline.local_score.model_router import ModelScoringRouter
from trader.recommendation.application.pipeline.local_score.unavailable_model import UnavailableModelCapability
from trader.recommendation.domain.publication.models import Strategy
from trader.training.infra.model_bundles.profile_publication import InspectActiveProfileBundles
from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _empty_config(tmp_path: Path) -> Path:
    shutil.copytree(PROJECT_ROOT / "config", tmp_path / "config")
    return tmp_path / "config/runtime.json"


@pytest.mark.parametrize("profile", ("v2", "v3", "both"))
@pytest.mark.parametrize("hour,minute", ((8, 0), (10, 0), (12, 0), (14, 59), (15, 10), (20, 30)))
def test_empty_clone_keeps_web_readable_and_models_inactive(tmp_path, monkeypatch, caplog, profile, hour, minute):
    config = _empty_config(tmp_path)
    now = datetime(2026, 10, 9, hour, minute, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr("trader.bootstrap._utc_now", lambda: now)
    started = []
    monkeypatch.setattr(threading.Thread, "start", lambda thread: started.append(thread.name))

    system = (
        BuildProfiledSystem(config, profiles=("v2", "v3"))
        if profile == "both"
        else BuildSystem(config, scoring_profile=profile)
    )

    client = system.app.test_client()
    assert client.get("/").status_code == 200
    for selected in ("v2", "v3") if profile == "both" else (profile,):
        payload = client.get(f"/api/status?profile={selected}").get_json()
        scoring = payload["scoring_profile"]
        assert scoring["profile_id"] == selected
        assert set(scoring["heads"]) == {"tomorrow", "d25"}
        for head in scoring["heads"].values():
            assert head["active"] is False
            assert head["model_hash"] == ""
            assert head["historical_failure_reasons"] == ["production_model_missing"]
        assert "./run.sh train" in caplog.text
    assert "./run.sh sync market" in caplog.text
    assert "缺少历史数据" in caplog.text
    assert started == []
    assert not (tmp_path / "data").exists()
    assert not (tmp_path / ".runtime").exists()


def test_missing_profile_does_not_disable_a_complete_other_profile(tmp_path, caplog):
    config = _empty_config(tmp_path)
    _publish_profile(tmp_path / "data/training/models", V3_TRAINING_PROFILE)
    system = BuildProfiledSystem(config)
    client = system.app.test_client()

    for profile, active in (("v2", False), ("v3", True)):
        scoring = client.get(f"/api/status?profile={profile}").get_json()["scoring_profile"]
        assert all(head["active"] is active for head in scoring["heads"].values())
    assert "V2 双头模型工件缺失" in caplog.text
    assert "V3 双头模型工件缺失" not in caplog.text


def test_partial_or_corrupt_profile_keeps_web_readable_with_classified_model_failure(tmp_path, monkeypatch):
    now = datetime(2026, 10, 20, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr("trader.bootstrap._utc_now", lambda: now)
    config = _empty_config(tmp_path)
    contract = V3_TRAINING_PROFILE.heads[0]
    _write_bundle(tmp_path / "data/training/models/v3" / contract.directory_name, contract)
    system = BuildSystem(config, scoring_profile="v3")
    scoring = system.app.test_client().get("/api/status").get_json()["scoring_profile"]
    assert all(head["active"] is False for head in scoring["heads"].values())
    original = record()
    system.records.initialize()
    system.records.commit(original)
    route = f"/api/decisions/tomorrow/history?date={original.trade_date.isoformat()}"
    before = system.app.test_client().get(route).get_json()

    _publish_profile(tmp_path / "data/training/models", V3_TRAINING_PROFILE)
    metadata = (
        InspectActiveProfileBundles(tmp_path / "data/training/models/v3", V3_TRAINING_PROFILE)[0].model_path.parent
        / "metadata.json"
    )
    metadata.write_text("{}", encoding="utf-8")
    degraded = BuildSystem(config, scoring_profile="v3")
    client = degraded.app.test_client()
    assert client.get("/").status_code == 200
    scoring = client.get("/api/status").get_json()["scoring_profile"]
    assert scoring["profile_id"] == "v3"
    for head in scoring["heads"].values():
        assert head["active"] is False
        assert head["historical_failure_reasons"] == ["production_model_integrity_conflict"]
    after = client.get(route).get_json()
    assert after == before
    assert after["items"][0]["code"] == original.decision.items[0].code


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

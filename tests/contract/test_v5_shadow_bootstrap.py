"""V5 composition keeps missing models and both freezers without production authority."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from tests.contract.test_learned_rank_bootstrap import _config
from trader.bootstrap import BuildSystem
from trader.recommendation.domain.publication.models import Strategy
from trader.web import CreateApp


@dataclass(frozen=True)
class _Clock:
    value: datetime

    def now(self) -> datetime:
        return self.value


def _reject_thread_start(_thread: threading.Thread) -> None:
    raise AssertionError("application construction started a thread")


def test_missing_v5_build_closes_both_freezers_and_preserves_read_only_status(tmp_path, monkeypatch):
    config = _config(tmp_path)
    monkeypatch.setattr(threading.Thread, "start", _reject_thread_start)

    system = BuildSystem(config, scoring_profile="v5")
    response = system.app.test_client().get("/api/status")
    assert response.status_code == 200
    scoring = response.get_json()["scoring_profile"]
    assert scoring["profile_id"] == "v5"
    assert set(scoring["heads"]) == {"tomorrow", "d25"}
    for head in scoring["heads"].values():
        assert head["is_enable"] is False
        assert head["production_authority"] is False
        assert head["activation_basis"] == "trained_artifact"

    now = datetime(2026, 10, 23, 15, 5, tzinfo=ZoneInfo("Asia/Shanghai"))
    freezers = system.scheduler._dependencies.sealing._freezers
    assert set(freezers) == {Strategy.TOMORROW, Strategy.D25}
    for strategy, freezer in freezers.items():
        assert system.tomorrow_index.snapshot(strategy).current is None
        monkeypatch.setattr(freezer, "_clock", _Clock(now))
        result = freezer.freeze_scheduled()
        assert result.status == "shadow_only"
        assert result.record is None
        assert not system.tomorrow_index.is_sealed(strategy, now.date())
        assert system.records.load(strategy, now.date()) is None

    assert not (tmp_path / "runtime").exists()
    assert not (tmp_path / "data/recommendation").exists()


@pytest.mark.parametrize("profile_id", ("v2", "v3"))
def test_existing_profiles_keep_production_authority_at_composition(tmp_path, monkeypatch, profile_id):
    config = _config(tmp_path)
    monkeypatch.setattr(threading.Thread, "start", _reject_thread_start)

    system = BuildSystem(config, scoring_profile=profile_id)
    response = system.app.test_client().get("/api/status")
    assert response.status_code == 200
    scoring = response.get_json()["scoring_profile"]
    assert scoring["profile_id"] == profile_id
    assert set(scoring["heads"]) == {"tomorrow", "d25"}
    assert all(head["production_authority"] is True for head in scoring["heads"].values())
    assert not (tmp_path / "runtime").exists()
    assert not (tmp_path / "data/recommendation").exists()


def test_create_app_stays_without_threads_or_runtime_data_writes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(threading.Thread, "start", _reject_thread_start)

    app = CreateApp()
    assert app.test_client().get("/").status_code == 200
    assert app.test_client().get("/api/status").status_code == 503
    assert list(tmp_path.iterdir()) == []

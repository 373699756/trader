"""Free profile combinations preserve one process, one preparation, and isolated delivery."""

import json
import threading
from itertools import combinations

import pytest

from tests.contract.test_cold_start import _empty_config
from trader import bootstrap
from trader.entrypoints.cli import BuildParser as CliParser
from trader.entrypoints.server import BuildParser as ServerParser
from trader.recommendation.domain.scoring.profile_identity import SCORING_PROFILE_IDS
from trader.recommendation.domain.scoring.profile_selection import ParseProfileSelection
from trader.training.entrypoints import commands, selected_training

_SELECTIONS = tuple(group for size in range(1, 5) for group in combinations(SCORING_PROFILE_IDS, size))


@pytest.mark.parametrize("profiles", _SELECTIONS)
def test_every_profile_combination_is_accepted_by_server_and_training(profiles):
    argument = ",".join(profiles)
    assert ParseProfileSelection(argument) == profiles
    assert ServerParser().parse_args([argument]).profiles == profiles
    assert CliParser().parse_args(["train", argument]).profiles == profiles


def test_omitted_selection_means_all_and_order_is_preserved():
    assert ServerParser().parse_args([]).profiles == SCORING_PROFILE_IDS
    assert CliParser().parse_args(["train"]).profiles == SCORING_PROFILE_IDS
    assert ParseProfileSelection("v5,v1,v2") == ("v5", "v1", "v2")


@pytest.mark.parametrize("selection", ("", "both", "v4", "v1,v1", "v2,", ",v3", "v1||v2", "v1|v2", "v1,,v5"))
def test_invalid_selection_is_rejected_before_any_runtime_work(selection):
    with pytest.raises(ValueError):
        ParseProfileSelection(selection)
    with pytest.raises(SystemExit) as caught:
        ServerParser().parse_args([selection])
    assert caught.value.code == 2
    with pytest.raises(SystemExit) as caught:
        CliParser().parse_args(["train", selection])
    assert caught.value.code == 2


@pytest.mark.parametrize("profiles", (("v1", "v2", "v3"), SCORING_PROFILE_IDS, ("v1", "v5"), ("v3",)))
def test_selected_profiles_share_resources_and_web_shows_only_selected_buttons(tmp_path, monkeypatch, profiles):
    config = _empty_config(tmp_path)
    monkeypatch.setattr(threading.Thread, "start", lambda self: pytest.fail("composition started a thread"))
    group = bootstrap.BuildProfiledSystem(config, profiles=profiles)
    default = "v2" if "v2" in profiles else profiles[0]
    primary = group.systems[0]
    assert primary.profile_id == default
    assert {system.profile_id for system in group.systems} == set(profiles)
    assert sum(system.owns_shared_resources for system in group.systems) == 1
    assert len({id(system.records) for system in group.systems}) == len(profiles)
    assert len({id(system.tomorrow_index) for system in group.systems}) == len(profiles)
    for system in group.systems:
        assert system.market_cache is primary.market_cache
        assert system.budget is primary.budget
        assert system.market_data.quotes.gateway is primary.market_data.quotes.gateway
        assert system.market_data.research is primary.market_data.research
    client = group.app.test_client()
    html = client.get("/").get_data(as_text=True)
    assert html.count('class="profile-tab') == len(profiles) + 1  # Includes the tablist class.
    profile_buttons = [line for line in html.splitlines() if 'data-profile="' in line]
    assert sum('aria-selected="true"' in line for line in profile_buttons) == 1
    for profile in SCORING_PROFILE_IDS:
        assert (f'data-profile="{profile}"' in html) == (profile in profiles)
        status = client.get(f"/api/status?profile={profile}")
        if profile in profiles:
            assert status.get_json()["scoring_profile"]["profile_id"] == profile
        else:
            assert status.status_code == 400
    assert f'name="trader-long-scoring-profile" content="{default}"' in html
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("profiles", _SELECTIONS)
def test_training_selection_prepares_history_once_and_runs_only_selected_profiles(
    tmp_path, monkeypatch, capsys, profiles
):
    config = _empty_config(tmp_path)
    calls = []

    def download(*_args, **_kwargs):
        calls.append("history")
        return 0

    monkeypatch.setattr("trader.download.entrypoints.commands.RunDownload", download)
    monkeypatch.setattr(commands, "ConfigureTrainingResources", lambda **_kwargs: None)
    monkeypatch.setattr(selected_training, "ConfigureTrainingResources", lambda **_kwargs: None)
    monkeypatch.setattr(commands, "RunJointTraining", lambda *_args, **_kwargs: calls.extend(("v2", "v3")) or 0)
    monkeypatch.setattr(
        "trader.training.entrypoints.v1.training.RunLearnedRankTraining",
        lambda *_args, **_kwargs: calls.append("v1") or 0,
    )
    monkeypatch.setattr(
        selected_training, "RunModelTrainingCommand", lambda _runtime, profile: calls.append(profile.profile_id) or 0
    )
    assert bootstrap.ExecuteDownloadTrain(config, scoring_profiles=profiles) == 0
    assert calls[0] == "history" and calls.count("history") == 1
    assert sorted(calls[1:]) == sorted(profiles)
    if len(profiles) > 1:
        assert json.loads(capsys.readouterr().out)["profiles"] == list(profiles)
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("failure", (1, 130, ModuleNotFoundError("private", name="torch")))
def test_training_failure_continues_other_profiles_but_cancellation_stops(monkeypatch, capsys, failure):
    calls = []

    def configure(*, scoring_profile):
        if isinstance(failure, Exception) and scoring_profile == "v5":
            raise failure

    def run_standard(_runtime, profile):
        calls.append(profile.profile_id)
        return failure if profile.profile_id == "v5" else 0

    monkeypatch.setattr(selected_training, "ConfigureTrainingResources", configure)
    monkeypatch.setattr(selected_training, "RunModelTrainingCommand", run_standard)
    exit_code = selected_training.RunSelectedTraining(
        None,
        ("v5", "v1"),
        run_v1=lambda: calls.append("v1") or 0,
        run_joint=lambda: pytest.fail("unselected profiles ran"),
    )
    report = json.loads(capsys.readouterr().out.splitlines()[-1])
    if failure == 130:
        assert exit_code == 130 and calls == ["v5"]
        assert report["not_started"] == ["v1"]
    else:
        assert exit_code == 1 and calls[-1] == "v1"
        assert report["not_started"] == []

"""Profile requests have one configuration source without weakening native admission."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier

import pytest

from trader.training.infra.resource_settings import LoadTrainingResourceSettings
from trader.training.application.resources import TrainingProfileThreads
from trader.training.infra.cpu_admission import TrainingCpuBudget

_ROOT = Path(__file__).resolve().parents[4]


def _configuration(directory: Path) -> dict:
    (directory / "config").mkdir()
    return json.loads((_ROOT / "config/training.json").read_text())


def _write(directory: Path, document: dict) -> None:
    (directory / "config/training.json").write_text(json.dumps(document))


@pytest.mark.parametrize("medium", ("hdd", "ssd", "unknown"))
def test_profile_requests_follow_selected_medium_and_defaults_stay_unchanged(tmp_path: Path, medium: str) -> None:
    document = _configuration(tmp_path)
    document["resources"]["ssd"]["cpu"]["profile_threads"] = {"v1": 1, "v2": 4, "v3": 3}
    _write(tmp_path, document)
    settings = LoadTrainingResourceSettings(tmp_path, medium)
    expected = TrainingProfileThreads(1, 4, 3) if medium == "ssd" else TrainingProfileThreads(2, 2, 2)
    budget = TrainingCpuBudget(4, settings.cpu)
    assert budget.profile_threads == expected
    assert (budget.capacity, budget.native_threads) == (3, 2)
    assert tuple(budget.profile_threads.for_profile(profile) for profile in ("v1", "v2", "v3")) == (
        expected.v1, expected.v2, expected.v3
    )
    with pytest.raises(ValueError, match="require_v1_v2_v3"):
        budget.profile_threads.for_profile("v5")
    fitting = next(profile for profile in document["profiles"] if profile["profile"] == "v5")["tcn"]
    assert fitting["compute_threads"] == 2
    assert fitting["epochs"] == 10


@pytest.mark.parametrize("medium", ("hdd", "ssd"))
@pytest.mark.parametrize("profile", ("v1", "v2", "v3"))
@pytest.mark.parametrize("invalid", (True, 0, -1, 1.5, "2", None))
def test_invalid_profile_requests_in_either_medium_are_rejected(
    tmp_path: Path, medium: str, profile: str, invalid: object
) -> None:
    document = _configuration(tmp_path)
    document["resources"][medium]["cpu"]["profile_threads"][profile] = invalid
    _write(tmp_path, document)
    with pytest.raises(ValueError):
        LoadTrainingResourceSettings(tmp_path, "hdd")


@pytest.mark.parametrize("mutation", ("missing_group", "missing_profile", "duplicate_v5_source", "not_mapping"))
def test_profile_thread_configuration_is_complete_and_rejects_a_second_v5_source(
    tmp_path: Path, mutation: str
) -> None:
    document = _configuration(tmp_path)
    cpu = document["resources"]["ssd"]["cpu"]
    if mutation == "missing_group":
        del cpu["profile_threads"]
    elif mutation == "missing_profile":
        del cpu["profile_threads"]["v3"]
    elif mutation == "duplicate_v5_source":
        cpu["profile_threads"]["v5"] = 1
    else:
        cpu["profile_threads"] = []
    _write(tmp_path, document)
    with pytest.raises(ValueError):
        LoadTrainingResourceSettings(tmp_path, "hdd")


def test_profile_requests_still_pay_native_floor_and_are_capped_by_machine() -> None:
    settings = LoadTrainingResourceSettings(_ROOT, "hdd").cpu
    settings = replace(settings, profile_threads=TrainingProfileThreads(1, 2, 1))
    budget = TrainingCpuBudget(4, settings)
    with budget.reserve(budget.profile_threads.v1, lambda: None) as granted:
        assert granted == 1
        assert budget.peak_reserved_threads == 2
    constrained = TrainingCpuBudget(1, settings)
    with constrained.reserve(constrained.profile_threads.v2, lambda: None) as granted:
        assert granted == 1
    assert constrained.peak_reserved_threads == 1
    assert constrained.profile_threads.v2 == 2


def test_explicit_native_one_candidate_can_overlap_two_plus_one_without_exceeding_budget() -> None:
    settings = LoadTrainingResourceSettings(_ROOT, "hdd").cpu
    budget = TrainingCpuBudget(4, replace(settings, native_threads=1))
    barrier = Barrier(2)

    def reserve(threads: int) -> int:
        with budget.reserve(threads, lambda: None) as granted:
            barrier.wait(timeout=5)
            return granted

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert tuple(executor.map(reserve, (budget.profile_threads.v2, 1))) == (2, 1)
    assert budget.peak_reserved_threads == 3
    with budget.reserve(3, lambda: None) as granted:
        assert granted == 3


@pytest.mark.parametrize("invalid", (True, 0, -1, 1.5, "2", None))
def test_profile_thread_type_rejects_invalid_direct_construction(invalid: object) -> None:
    with pytest.raises(ValueError, match="training_profile_threads_invalid"):
        TrainingProfileThreads(invalid, 2, 2)

"""Joint-profile training keeps one history lease and one sample read pass."""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from trader.infra.atomic_files.json import AtomicWriteJson
from trader.training.infra import engine
from trader.training.infra.engine import HeadTrainingResult, ProfileTrainingRequest, TrainingRunResult
from trader.training.infra.profile.v2.contracts import V2_TRAINING_PROFILE
from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE
from trader.training.infra.training_settings import LoadProfileTrainingSettings

PROJECT_ROOT = Path(__file__).resolve().parents[4]


def _requests(root: Path) -> tuple[ProfileTrainingRequest, ProfileTrainingRequest]:
    return tuple(
        ProfileTrainingRequest(
            root / "history",
            root / "train",
            profile,
            profile.heads,
            LoadProfileTrainingSettings(PROJECT_ROOT, profile.profile_id),
        )
        for profile in (V2_TRAINING_PROFILE, V3_TRAINING_PROFILE)
    )  # type: ignore[return-value]


def _ready_result(request: ProfileTrainingRequest) -> TrainingRunResult:
    heads = tuple(
        HeadTrainingResult(
            head.strategy,
            "engineering_ready",
            "complete_manifest",
            "run",
            "a" * 64,
            1,
            1,
            "b" * 64,
            "c" * 64,
            1,
            1,
            1,
            (),
        )
        for head in request.contracts
    )
    return TrainingRunResult("run", "a" * 64, 1, heads)


@pytest.mark.parametrize("parallel_fit", (False, True))
@pytest.mark.parametrize("fail_v2", (False, True))
def test_joint_training_prepares_both_profiles_before_fitting_and_releases_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, parallel_fit: bool, fail_v2: bool
) -> None:
    requests = _requests(tmp_path)
    (tmp_path / "history").mkdir()
    archive = SimpleNamespace(
        archive_root=tmp_path / "history",
        snapshot=SimpleNamespace(active_snapshot_hash="a" * 64, training_codes=("600000",)),
    )
    monkeypatch.setattr(engine.SQLiteHistoryTrainingInputSnapshot, "open", lambda _root: archive)
    monkeypatch.setattr(engine, "RequireHistoryPartitionRepackInactive", lambda _root: None)
    monkeypatch.setattr(engine, "_prepare_locked_split", lambda *_args: (SimpleNamespace(content_hash="b" * 64), ""))
    monkeypatch.setattr(
        engine, "_require_industry_coverage", lambda *_args: SimpleNamespace(readable_dates=frozenset())
    )

    class SampleCache:
        reused = False

        def open(self):
            return nullcontext(SimpleNamespace(count=lambda: 1))

        def seal(self, _samples) -> None:
            pass

    monkeypatch.setattr(engine, "_sample_cache_for", lambda *_args: SampleCache())
    observed: list[str] = []

    def build(samples) -> int:
        assert len(samples) == 2
        observed.append("shared_scan")
        return 17

    def fit(_archive, request: ProfileTrainingRequest) -> TrainingRunResult:
        observed.append(request.profile.profile_id)
        assert observed[0] == "shared_scan"
        if fail_v2 and request.profile.profile_id == "v2":
            return TrainingRunResult(None, "a" * 64, 0, ())
        return _ready_result(request)

    monkeypatch.setattr(engine, "BuildJointTrainingSamples", build)
    monkeypatch.setattr(engine, "_run_locked", fit)

    result = engine.RunJointProfileTraining(requests, parallel_fit=parallel_fit)

    assert observed[0] == "shared_scan"
    assert set(observed[1:]) == {"v2", "v3"}
    assert result.history_scan_passes == 1
    assert result.history_rows_read == 17
    assert result.fit_mode == ("parallel" if parallel_fit else "serial")
    assert result.status == ("blocked" if fail_v2 else "engineering_ready")
    assert tuple(profile.status for profile in result.profiles) == (
        "blocked" if fail_v2 else "engineering_ready",
        "engineering_ready",
    )
    with engine.HistoryMaintenanceLock(tmp_path / "history" / ".maintenance.lock"):
        pass


def test_joint_training_rejects_duplicate_profile_without_touching_history(tmp_path: Path) -> None:
    requests = _requests(tmp_path)
    with pytest.raises(ValueError, match="joint_training_requires_v2_v3"):
        engine.RunJointProfileTraining((requests[0], requests[0]), parallel_fit=False)


def test_joint_matching_artifacts_skip_sample_scan_and_fit_under_history_lock(tmp_path, monkeypatch):
    requests = _requests(tmp_path)
    (tmp_path / "history").mkdir()
    snapshot = SimpleNamespace(active_snapshot_hash="a" * 64)
    monkeypatch.setattr(
        engine.SQLiteHistoryTrainingInputSnapshot,
        "open",
        lambda root: SimpleNamespace(archive_root=root, snapshot=snapshot),
    )
    monkeypatch.setattr(engine, "RequireHistoryPartitionRepackInactive", lambda root: None)
    monkeypatch.setattr(engine, "_reuse_profile", lambda snapshot, request: _ready_result(request))
    monkeypatch.setattr(engine, "BuildJointTrainingSamples", lambda *_: pytest.fail("must not read prices"))
    monkeypatch.setattr(engine, "_run_locked", lambda *_: pytest.fail("must not refit"))
    result = engine.RunJointProfileTraining(requests, parallel_fit=False, reuse_matching=True)
    assert result.reused and result.status == "engineering_ready"
    assert result.history_scan_passes == result.history_rows_read == result.fit_seconds == 0


@pytest.mark.parametrize("mismatch", ("none", "input", "cutoff", "settings", "implementation", "corrupt"))
def test_reuse_requires_valid_bundles_and_current_input_contract(tmp_path, monkeypatch, mismatch):
    from dataclasses import replace
    from datetime import date

    request = _requests(tmp_path)[0]
    cutoff = date(2026, 10, 16)
    snapshot = SimpleNamespace(active_snapshot_hash="a" * 64)
    monkeypatch.setattr(engine, "_mature_label_cutoff", lambda *_: cutoff)
    bundles = tuple(
        SimpleNamespace(
            training_input_hash="a" * 64,
            label_cutoff=cutoff,
            training_contract_hash=engine._training_contract_hash(request.profile, head, request.settings),
            training_run_hash="run",
            report_hash="b" * 64,
            model_hash="c" * 64,
            model=SimpleNamespace(
                training_input_codes=1, training_universe_codes=1, industries=(), training_rows=10, validation_rows=2
            ),
        )
        for head in request.contracts
    )

    def inspect(*_):
        if mismatch == "corrupt":
            raise ValueError("bundle hash mismatch")
        return bundles

    monkeypatch.setattr(engine, "InspectActiveProfileBundles", inspect)
    if mismatch == "input":
        snapshot.active_snapshot_hash = "d" * 64
    elif mismatch == "cutoff":
        bundles[0].label_cutoff = date(2026, 10, 15)
    elif mismatch == "settings":
        request = replace(request, settings=replace(request.settings, fit_dates=request.settings.fit_dates + 1))
    elif mismatch == "implementation":
        monkeypatch.setattr(engine, "TrainingImplementationIdentity", lambda: "changed")
    result = engine._reuse_profile(snapshot, request)
    assert (result is not None) == (mismatch == "none")


def test_joint_training_missing_history_blocks_both_without_publishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(_root: Path) -> None:
        raise engine.HistoryTrainingInputError("history_manifest_unavailable")

    monkeypatch.setattr(engine.SQLiteHistoryTrainingInputSnapshot, "open", unavailable)

    result = engine.RunJointProfileTraining(_requests(tmp_path), parallel_fit=False)

    assert result.history_scan_passes == 0
    assert result.history_rows_read == 0
    assert all(profile.status == "blocked" for profile in result.profiles)
    assert not (tmp_path / "train").exists()


def test_joint_fit_mode_keeps_rotating_disk_serial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine, "ClassifyHistoryStorage", lambda _root: "hdd")
    assert engine.ChooseJointFitMode(tmp_path) == "serial"


def test_joint_fit_mode_allows_parallel_only_with_cpu_memory_and_scope_headroom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine, "ClassifyHistoryStorage", lambda _root: "ssd")
    monkeypatch.setattr(engine.os, "cpu_count", lambda: 4)
    original_read_text = Path.read_text

    def read_text(path: Path, *args, **kwargs) -> str:
        if str(path) == "/proc/meminfo":
            return "MemAvailable: 6000000 kB\n"
        if str(path) == "/sys/fs/cgroup/memory.max":
            return "5368709120\n"
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    evidence_path = tmp_path / "joint-fit-evidence.json"
    AtomicWriteJson(
        evidence_path,
        {
            "schema_version": "joint_fit_benchmark",
            "status": "passed",
            "storage_kind": "ssd",
            "cpu_count": 4,
            "serial_input_hash": "a" * 64,
            "parallel_input_hash": "a" * 64,
            "serial_fit_seconds": 100.0,
            "parallel_fit_seconds": 80.0,
            "parallel_peak_rss_bytes": 4 * 1024**3,
        },
    )
    assert engine.ChooseJointFitMode(tmp_path) == "serial"
    assert engine.ChooseJointFitMode(tmp_path, evidence_path=evidence_path) == "parallel"
    monkeypatch.setattr(engine.os, "cpu_count", lambda: 2)
    assert engine.ChooseJointFitMode(tmp_path, evidence_path=evidence_path) == "serial"

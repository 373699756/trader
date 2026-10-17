from __future__ import annotations

from pathlib import Path

import pytest

from scripts import check_joint_training_performance
from scripts.check_joint_training_performance import BuildParser, Main, _external_path
from trader.training.infra.joint_fit_evidence import LoadJointFitEvidence


def test_joint_benchmark_requires_explicit_external_paths() -> None:
    parser = BuildParser()
    args = parser.parse_args(
        [
            "--history-root",
            "/tmp/history-copy",
            "--workspace-root",
            "/tmp/joint-benchmark",
            "--output",
            "/tmp/joint-benchmark.json",
        ]
    )
    assert args.history_root == Path("/tmp/history-copy")
    with pytest.raises(ValueError, match="external"):
        _external_path(Path(__file__).resolve().parents[3] / "data" / "history")
    with pytest.raises(ValueError, match="absolute"):
        _external_path(Path("data/history"))


def test_joint_benchmark_never_writes_evidence_into_history_copy(tmp_path: Path) -> None:
    history = tmp_path / "history"
    workspace = tmp_path / "work"
    with pytest.raises(ValueError, match="output must be separate"):
        Main(
            [
                "--history-root",
                str(history),
                "--workspace-root",
                str(workspace),
                "--output",
                str(history / "evidence.json"),
            ]
        )
    assert not history.exists()


def test_joint_benchmark_resource_rejection_writes_unusable_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    history = tmp_path / "history"
    workspace = tmp_path / "work"
    output = tmp_path / "evidence.json"
    monkeypatch.setattr(check_joint_training_performance, "ParallelFitResourcesAvailable", lambda _root: False)

    status = Main(
        [
            "--history-root",
            str(history),
            "--workspace-root",
            str(workspace),
            "--output",
            str(output),
        ]
    )

    assert status == 2
    assert LoadJointFitEvidence(output) is None
    assert not history.exists()
    assert not workspace.exists()

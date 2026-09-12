from pathlib import Path
from types import SimpleNamespace

from scripts import check_tomorrow_training_memory


def test_training_memory_gate_requires_explicit_roots_and_reports_peak_rss(tmp_path: Path, monkeypatch, capsys) -> None:
    observed: list[tuple[Path, Path, str]] = []

    def train(history: Path, output: Path, *, source_commit: str, progress):
        del progress
        observed.append((history, output, source_commit))
        return SimpleNamespace(
            status="engineering_ready",
            training_input_hash="a" * 64,
            model_hash="b" * 64,
            report_hash="c" * 64,
            failure_reasons=(),
        )

    monkeypatch.setattr(check_tomorrow_training_memory, "run_tomorrow_training", train)
    monkeypatch.setattr(check_tomorrow_training_memory, "_peak_rss_bytes", lambda: 100)
    monkeypatch.setattr(check_tomorrow_training_memory.os, "nice", lambda _increment: None)
    history = tmp_path / "history"
    output = tmp_path / "train"

    assert (
        check_tomorrow_training_memory.main(
            [
                "--history-root",
                str(history),
                "--train-root",
                str(output),
                "--source-commit",
                "d" * 40,
                "--max-rss-mib",
                "1",
            ]
        )
        == 0
    )
    assert observed == [(history.resolve(), output.resolve(), "d" * 40)]
    assert '"peak_rss_bytes": 100' in capsys.readouterr().out


def test_training_memory_gate_applies_the_shared_three_thread_policy(monkeypatch) -> None:
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        monkeypatch.delenv(name, raising=False)

    check_tomorrow_training_memory._configure_resources()

    assert check_tomorrow_training_memory.TOMORROW_TRAINING_PEAK_RSS_MIB == 4_096
    assert {
        name: check_tomorrow_training_memory.os.environ[name]
        for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS")
    } == {
        "OMP_NUM_THREADS": "3",
        "OPENBLAS_NUM_THREADS": "3",
        "MKL_NUM_THREADS": "3",
        "NUMEXPR_NUM_THREADS": "3",
    }

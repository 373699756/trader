from pathlib import Path
from types import SimpleNamespace

from scripts import check_tomorrow_training_memory


def test_training_memory_gate_requires_explicit_roots_and_reports_peak_rss(tmp_path: Path, monkeypatch, capsys) -> None:
    observed: list[tuple[Path, Path, str]] = []

    def train(history: Path, output: Path, *, source_commit: str):
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

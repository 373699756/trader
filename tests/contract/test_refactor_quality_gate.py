from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts import check_refactor_quality as quality


@pytest.fixture
def gate_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    scripts = tmp_path / "scripts"
    source = tmp_path / "src" / "trader"
    scripts.mkdir()
    source.mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (scripts / "gate.py").write_text("", encoding="utf-8")
    (scripts / "README.md").write_text(
        "## Retained tools\n\n"
        + quality.DECLARATION_HEADER
        + "\n| --- | --- | --- | --- | --- | --- |\n"
        + "| `gate.py` | Quality | None | None | stdout | 60 s per child |\n",
        encoding="utf-8",
    )
    (source / "core.py").write_text(
        "class Allowed:\n    def work(self, a, b, c, d, e, f):\n        return (a, b, c, d, e, f)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(quality, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(quality, "SOURCE_ROOT", source)
    monkeypatch.setattr(quality, "NAMING_ROOTS", (source, scripts, tmp_path / "tests"))
    monkeypatch.setattr(quality, "TOP_LEVEL_SCRIPT_MANIFEST", frozenset({"gate.py"}))
    monkeypatch.setattr(quality, "EXPECTED_DEBT", frozenset({("PLR0913", "core.py", "Allowed.work")}))
    return tmp_path


def test_real_ruff_gate_accepts_baseline_without_repository_writes(gate_repository: Path) -> None:
    before = {
        path.relative_to(gate_repository): path.read_bytes() for path in gate_repository.rglob("*") if path.is_file()
    }

    assert quality.main() == 0

    after = {
        path.relative_to(gate_repository): path.read_bytes() for path in gate_repository.rglob("*") if path.is_file()
    }
    assert after == before


@pytest.mark.parametrize("relocation", ("file", "class", "function"))
def test_same_count_debt_relocation_fails_public_gate(gate_repository: Path, relocation: str) -> None:
    source = gate_repository / "src" / "trader" / "core.py"
    if relocation == "file":
        source.rename(source.with_name("moved.py"))
    else:
        original, changed = ("Allowed", "Moved") if relocation == "class" else ("work", "moved")
        source.write_text(source.read_text(encoding="utf-8").replace(original, changed), encoding="utf-8")

    assert quality.main() == 1


@pytest.mark.parametrize("fault", ("empty_field", "missing_row", "duplicate_row", "unknown_script", "missing_section"))
def test_incomplete_inventory_fails_public_gate(gate_repository: Path, fault: str) -> None:
    readme = gate_repository / "scripts" / "README.md"
    text = readme.read_text(encoding="utf-8")
    if fault == "unknown_script":
        (readme.parent / "extra.py").write_text("", encoding="utf-8")
    elif fault == "empty_field":
        text = text.replace("| None | None |", "| None | |")
    elif fault == "missing_row":
        text = text[: text.index("| `gate.py`")]
    elif fault == "duplicate_row":
        text += text.splitlines()[-1] + "\n"
    else:
        text = text.replace("## Retained tools", "## Removed section")
    readme.write_text(text, encoding="utf-8")

    assert quality.main() == 1


@pytest.mark.parametrize("stdout", ("", "{}", "[null]", '[{"code":"C901"}]', "[]"))
def test_invalid_ruff_output_is_not_accepted(
    gate_repository: Path, monkeypatch: pytest.MonkeyPatch, stdout: str
) -> None:
    monkeypatch.setattr(
        quality.subprocess, "run", lambda *_args, **_kwargs: subprocess.CompletedProcess((), 1, stdout, "")
    )

    assert quality.main() == 2


@pytest.mark.parametrize("failed_call", (1, 2))
def test_each_ruff_timeout_fails_closed_without_forwarding_raw_output(
    gate_repository: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], failed_call: int
) -> None:
    calls = 0
    diagnostics = json.dumps(
        [{"code": "PLR0913", "filename": str(gate_repository / "src/trader/core.py"), "location": {"row": 2}}]
    )

    def run(command: tuple[str, ...], **options: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        assert options["timeout"] == 60
        assert "--no-cache" in command
        if calls == failed_call:
            raise subprocess.TimeoutExpired(command, 60, output="private raw output", stderr="private raw error")
        return subprocess.CompletedProcess(command, 1, diagnostics, "")

    monkeypatch.setattr(quality.subprocess, "run", run)

    assert quality.main() == 2
    captured = capsys.readouterr()
    assert "deadline" in captured.err
    assert "private raw" not in captured.out + captured.err

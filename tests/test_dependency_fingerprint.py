import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "dependency_fingerprint.py"


def _run(root, command):
    return subprocess.run(
        [sys.executable, str(_SCRIPT), command, "--root", str(root)],
        capture_output=True,
        check=False,
        text=True,
    )


def test_dependency_marker_changes_when_runtime_declaration_changes(tmp_path):
    (tmp_path / "requirements").mkdir()
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "pytest"\n', encoding="utf-8")
    runtime = tmp_path / "requirements" / "runtime.txt"
    runtime.write_text("-e .\n", encoding="utf-8")

    assert _run(tmp_path, "check").returncode == 1
    assert _run(tmp_path, "write").returncode == 0
    assert _run(tmp_path, "check").returncode == 0

    runtime.write_text("-e .\n# dependency changed\n", encoding="utf-8")

    assert _run(tmp_path, "check").returncode == 1


def test_dependency_marker_rejects_matching_fingerprint_when_project_is_not_installed(tmp_path):
    (tmp_path / "requirements").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "definitely-not-an-installed-trader-test-package"\n',
        encoding="utf-8",
    )
    (tmp_path / "requirements" / "runtime.txt").write_text("-e .\n", encoding="utf-8")

    assert _run(tmp_path, "write").returncode == 0
    assert _run(tmp_path, "check").returncode == 1

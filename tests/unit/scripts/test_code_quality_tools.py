from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path

import pytest

from scripts.code_quality.cli import Main
from scripts.code_quality.files import Change, ReadSources, SourceFile, WriteChanges
from scripts.code_quality.formatting import FormatSource
from scripts.code_quality.manifest import ParseManifest
from scripts.code_quality.naming import Audit, NameIssues, Symbols, Words

CONFIG = Path(__file__).resolve().parents[3] / "pyproject.toml"


def _format(text: str) -> str:
    return FormatSource(SourceFile("scripts/sample.py", text.encode()), CONFIG).text


@pytest.mark.parametrize("count", [0, 1, 4, 5, 6])
def test_parameter_threshold_and_idempotence(count: int) -> None:
    args = ", ".join(f"arg{index}" for index in range(count))
    source = f"def Load({args}):\n    pass\n\nLoad({args})\n"
    result = _format(source)
    assert ("def Load(\n" in result) == (count >= 5)
    assert ("\nLoad(\n" in result) == (count >= 5)
    assert ast.dump(ast.parse(result)) == ast.dump(ast.parse(source))
    assert _format(result) == result


def test_short_parameters_collapse_and_exports_expand() -> None:
    result = _format('def Load(\n    one,\n    two,\n):\n    pass\n__all__ = ["Load", "Prices"]\n')
    assert "def Load(one, two):" in result
    assert '__all__ = [\n    "Load",\n    "Prices",\n]' in result


@pytest.mark.parametrize(
    "source",
    [
        'async def Load(self, a, /, b, *, c, **kwargs):\n    return "中文;文本"\n',
        'result = Load(Other(1, 2, 3, 4, 5), 2, 3, 4, key="中文")\n',
        'result = (lambda value: value)("中文")\n',
        "result = sum(item for item in items)\n",
        "result = Load(1,  # first\n 2,\n)\n",
        'values = [1, 2, 3]\ntext = """多行\n文字"""\n',
    ],
)
def test_complex_syntax_preserves_ast_and_comments(source: str) -> None:
    result = _format(source)
    assert ast.dump(ast.parse(result)) == ast.dump(ast.parse(source))


def test_dynamic_exports_and_long_files_refused() -> None:
    with pytest.raises(ValueError, match="dynamic __all__"):
        _format("__all__ = BuildExports()\n")
    source = SourceFile("src/trader/long.py", ("# retained\n" * 1501).encode())
    with pytest.raises(ValueError, match="1500"):
        FormatSource(source, CONFIG)


@pytest.mark.parametrize(
    "name",
    [
        "active",
        "_active",
        "ActiveSnapshot",
        "is_active",
        "ACTIVE_COUNT",
        "PriceStore",
        "company_repository",
        "StoredRow",
        "Archive",
        "REGISTRY",
    ],
)
def test_banned_word_variants(name: str) -> None:
    assert any(rule == "NAME002" for rule, _, _ in NameIssues(name, "variable"))


@pytest.mark.parametrize("name", ["RestorePrices", "inactive", "reactive", "Storage", "Models", "Artifacts"])
def test_no_substring_bans(name: str) -> None:
    assert not any(rule == "NAME002" for rule, _, _ in NameIssues(name, "class"))


def test_name_categories_and_external_literals() -> None:
    assert Words("HTTPActiveCache") == ("http", "active", "cache")
    assert NameIssues("class", "variable")[0][0] == "NAME001"
    assert any(rule == "NAME003" for rule, _, _ in NameIssues("list", "parameter"))
    assert any(severity == "review" for _, severity, _ in NameIssues("Artifacts", "class"))
    source = SourceFile(
        "scripts/example.py", b'from external import Registry\nvalue = {"active": "store"}\n# ActiveStore\n'
    )
    assert not Audit(source)
    findings = Audit(SourceFile("scripts/example.py", b'__all__ = ["_helper"]\n'))
    assert any(f.rule == "NAME010" for f in findings)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "src/trader").mkdir(parents=True)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src/trader/__init__.py").write_text("", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    return tmp_path


def _manifest(root: Path, path: str, name: str, new: str, scope: str = "", kind: str = "function") -> Path:
    sources = ReadSources(root, ("src/trader", "scripts", "tests"))
    symbols = Symbols((root / path).read_text())
    symbol = next(s for s in symbols if s.name == name and s.scope == scope and s.kind == kind)
    head = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    document = {
        "version": 1,
        "base_commit": head,
        "files": [{"path": s.path, "sha256": s.digest} for s in sources],
        "renames": [
            {
                "path": path,
                "scope": scope,
                "kind": kind,
                "old": name,
                "new": new,
                "line": symbol.line,
                "column": symbol.column,
                "visibility": "internal" if new.startswith("_") else "public",
                "reason": "confirmed fixture interface",
            }
        ],
        "references": [],
    }
    output = root / "manifest.json"
    output.write_text(json.dumps(document), encoding="utf-8")
    return output


def test_format_cli_preview_and_explicit_write(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = workspace / "scripts/example.py"
    path.write_text("result=Load(1,2,3,4,5)\n")
    before = path.read_bytes()
    assert Main(["--format", "preview", "scripts/example.py"], root=workspace) == 1
    assert "--- scripts/example.py" in capsys.readouterr().out
    assert path.read_bytes() == before
    assert Main(["--format", "write", "scripts/example.py"], root=workspace) == 0
    assert "Load(\n" in path.read_text()
    assert Main(["--format", "check", "scripts/example.py"], root=workspace) == 0


def test_rename_cross_module_preview_then_write(workspace: Path) -> None:
    source = workspace / "src/trader/prices.py"
    caller = workspace / "tests/consumer.py"
    source.write_text("def OldPrice(value):\n    return value + 1\n")
    caller.write_text("from trader.prices import OldPrice\nresult = OldPrice(value=2)\n")
    manifest = _manifest(workspace, "src/trader/prices.py", "OldPrice", "LoadPrice")
    before = source.read_bytes(), caller.read_bytes()
    assert Main(["--rename", "preview", "--manifest", str(manifest)], root=workspace) == 1
    assert before == (source.read_bytes(), caller.read_bytes())
    assert not (workspace / ".ropeproject").exists()
    assert Main(["--rename", "write", "--manifest", str(manifest)], root=workspace) == 0
    assert "def LoadPrice" in source.read_text()
    assert "LoadPrice(value=2)" in caller.read_text()
    assert Main(["--rename", "write", "--manifest", str(manifest)], root=workspace) == 2


def test_rename_parameter_only_correct_scope(workspace: Path) -> None:
    source = workspace / "scripts/example.py"
    source.write_text("def Load(old):\n    return old\ndef Other(old):\n    return old\nLoad(old=1)\nOther(old=2)\n")
    manifest = _manifest(workspace, "scripts/example.py", "old", "value", "Load", "parameter")
    assert Main(["--rename", "write", "--manifest", str(manifest)], root=workspace) == 0
    assert "Load(value=1)" in source.read_text()
    assert "Other(old=2)" in source.read_text()


@pytest.mark.parametrize("fault", ["hash", "commit", "collision", "banned", "string"])
def test_rename_rejects_before_any_original_write(workspace: Path, fault: str) -> None:
    source = workspace / "scripts/example.py"
    source.write_text(
        "def OldPrice(value):\n    return value\ndef OtherPrice():\n    pass\n"
        + ('entry = "OldPrice"\n' if fault == "string" else "")
    )
    manifest = _manifest(
        workspace,
        "scripts/example.py",
        "OldPrice",
        "ActivePrice" if fault == "banned" else "OtherPrice" if fault == "collision" else "LoadPrice",
    )
    if fault == "hash":
        source.write_text(source.read_text() + "# later edit\n")
    if fault == "commit":
        document = json.loads(manifest.read_text())
        document["base_commit"] = "obsolete"
        manifest.write_text(json.dumps(document))
    before = source.read_bytes()
    assert Main(["--rename", "write", "--manifest", str(manifest)], root=workspace) == 2
    assert source.read_bytes() == before


def test_path_and_concurrent_edit_protection(workspace: Path) -> None:
    source = workspace / "scripts/example.py"
    source.write_text("value = 1\n")
    with pytest.raises(ValueError, match="unsafe"):
        ReadSources(workspace, ("../example.py",))
    sources = ReadSources(workspace, ("scripts",))
    source.write_text("value = 2\n")
    with pytest.raises(ValueError, match="changed"):
        WriteChanges(workspace, sources, (Change(sources[0], "value = 3\n"),))
    assert source.read_text() == "value = 2\n"
    (workspace / "scripts/link.py").symlink_to(source)
    with pytest.raises(ValueError, match="symlink"):
        ReadSources(workspace, ("scripts",))


def test_manifest_strict_schema() -> None:
    with pytest.raises(ValueError):
        ParseManifest('{"version": 1}')


@pytest.mark.parametrize(
    "kind,old,new,scope", [("method", "load_old", "load_prices", "Prices"), ("attribute", "_old", "_prices", "Prices")]
)
def test_methods_and_attributes(workspace: Path, kind: str, old: str, new: str, scope: str) -> None:
    path = workspace / "scripts/example.py"
    path.write_text(
        "class Prices:\n    def __init__(self):\n        self._old = 1\n    def load_old(self):\n        return self._old\nresult = Prices().load_old()\n"
    )
    manifest = _manifest(workspace, "scripts/example.py", old, new, scope, kind)
    assert Main(["--rename", "write", "--manifest", str(manifest)], root=workspace) == 0
    assert all(symbol.name != old for symbol in Symbols(path.read_text()))


def test_exports_and_explicit_string_reference(workspace: Path) -> None:
    path = workspace / "scripts/example.py"
    path.write_text('def OldPrice():\n    pass\n__all__ = ["OldPrice"]\nentry = "module.OldPrice"\n')
    manifest = _manifest(workspace, "scripts/example.py", "OldPrice", "LoadPrice")
    document = json.loads(manifest.read_text())
    document["references"] = [
        {
            "path": "scripts/example.py",
            "line": 4,
            "old": "module.OldPrice",
            "new": "module.LoadPrice",
            "reason": "registered entry",
        }
    ]
    manifest.write_text(json.dumps(document))
    assert Main(["--rename", "write", "--manifest", str(manifest)], root=workspace) == 0
    assert "OldPrice" not in path.read_text()


def test_batch_relocates_child_after_parent_rename(workspace: Path) -> None:
    path = workspace / "scripts/example.py"
    path.write_text("class OldPrice:\n    def load_old(self):\n        return 1\nresult = OldPrice().load_old()\n")
    manifest = _manifest(workspace, "scripts/example.py", "OldPrice", "PriceWindow", kind="class")
    document = json.loads(manifest.read_text())
    symbol = next(s for s in Symbols(path.read_text()) if s.name == "load_old")
    document["renames"].append(
        {
            "path": "scripts/example.py",
            "scope": "OldPrice",
            "kind": "method",
            "old": "load_old",
            "new": "load_prices",
            "line": symbol.line,
            "column": symbol.column,
            "visibility": "public",
            "reason": "fixture method",
        }
    )
    manifest.write_text(json.dumps(document))
    assert Main(["--rename", "write", "--manifest", str(manifest)], root=workspace) == 0
    assert "PriceWindow().load_prices()" in path.read_text()


def test_inherited_method_is_refused(workspace: Path) -> None:
    path = workspace / "scripts/example.py"
    path.write_text("class Prices(object):\n    def load_old(self):\n        return 1\n")
    manifest = _manifest(workspace, "scripts/example.py", "load_old", "load_prices", "Prices", "method")
    assert Main(["--rename", "write", "--manifest", str(manifest)], root=workspace) == 2
    assert "load_old" in path.read_text()


def test_rename_preserves_crlf(workspace: Path) -> None:
    path = workspace / "scripts/example.py"
    path.write_bytes(b"def OldPrice():\r\n    return 1\r\n")
    manifest = _manifest(workspace, "scripts/example.py", "OldPrice", "LoadPrice")
    assert Main(["--rename", "write", "--manifest", str(manifest)], root=workspace) == 0
    assert path.read_bytes() == b"def LoadPrice():\r\n    return 1\r\n"


def test_audit_json_is_read_only(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = workspace / "scripts/example.py"
    path.write_text("active_count = 1\n")
    before = path.read_bytes()
    assert Main(["--naming-audit", "--all", "--json"], root=workspace) == 1
    output = json.loads(capsys.readouterr().out)
    assert any(f["rule"] == "NAME002" for f in output["findings"])
    assert output["manifest_template"]["files"]
    assert path.read_bytes() == before


def test_1500_line_boundary() -> None:
    text = "# retained\n" * 1500
    assert FormatSource(SourceFile("src/trader/example.py", text.encode()), CONFIG).text == text


def test_single_export_tuple_preserves_required_comma() -> None:
    assert ast.dump(ast.parse(_format('__all__ = ("Load",)\n'))) == ast.dump(ast.parse('__all__ = ("Load",)\n'))


def test_worker_timeout_and_version_failure_do_not_write(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.code_quality import cli

    path = workspace / "scripts/example.py"
    path.write_text("def OldPrice():\n    pass\n")
    manifest = _manifest(workspace, "scripts/example.py", "OldPrice", "LoadPrice")
    before = path.read_bytes()
    original_run = cli.subprocess.run

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "scripts.code_quality.rename_worker" in command:
            raise subprocess.TimeoutExpired(command, 300)
        return original_run(command, **kwargs)

    monkeypatch.setattr(cli.subprocess, "run", run)
    assert Main(["--rename", "write", "--manifest", str(manifest)], root=workspace) == 2
    monkeypatch.setattr(cli, "version", lambda name: "0.0")
    assert Main(["--format", "write", "scripts/example.py"], root=workspace) == 2
    assert path.read_bytes() == before


def test_audit_exception_and_pattern_bindings() -> None:
    text = "try:\n    Work()\nexcept ValueError as active_error:\n    pass\nmatch value:\n    case {'key': active_value, **active_rest}:\n        pass\n"
    findings = Audit(SourceFile("scripts/example.py", text.encode()))
    assert {f.name for f in findings if f.rule == "NAME002"} == {"active_error", "active_value", "active_rest"}


def test_private_constants_are_not_discarded_variables() -> None:
    assert not Audit(SourceFile("scripts/example.py", b"_MAX_ATTEMPTS = 3\n"))


def test_entry_metadata_blocks_python_only_rename(workspace: Path) -> None:
    path = workspace / "src/trader/example.py"
    path.write_text("def OldPrice():\n    pass\n")
    (workspace / "pyproject.toml").write_text('[project.scripts]\nentry = "trader.example:OldPrice"\n')
    manifest = _manifest(workspace, "src/trader/example.py", "OldPrice", "LoadPrice")
    assert Main(["--rename", "write", "--manifest", str(manifest)], root=workspace) == 2
    assert "OldPrice" in path.read_text()


def test_partial_io_failure_reports_completed_files(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.code_quality import files

    for name in ("one.py", "two.py"):
        (workspace / "scripts" / name).write_text("value = 1\n")
    sources = ReadSources(workspace, ("scripts",))
    original_replace = files.os.replace

    def replace(source: Path, target: Path) -> None:
        if target.name == "two.py":
            raise OSError("fixture disk failure")
        original_replace(source, target)

    monkeypatch.setattr(files.os, "replace", replace)
    with pytest.raises(ValueError, match="completed=.*one.py.*failed=scripts/two.py"):
        WriteChanges(workspace, sources, tuple(Change(s, "value = 2\n") for s in sources))
    assert (workspace / "scripts/one.py").read_text() == "value = 2\n"
    assert (workspace / "scripts/two.py").read_text() == "value = 1\n"
    assert not tuple((workspace / "scripts").glob(".quality-*"))

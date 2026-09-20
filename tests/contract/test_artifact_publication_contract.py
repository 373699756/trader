"""Immutable artifact publication must have exactly one implementation.

``os.link`` is what gives publication its create-or-conflict semantics: a path that
already exists is never overwritten. Every artifact family must reuse
``trader.infra.serialization.sealing`` so the temporary-file, flush and hard-link mechanics
stay reviewable in one place.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = ROOT / "src/trader"
SEALING = SOURCE_ROOT / "infra/serialization/sealing.py"

# ``infra/persistence/decision_records.py`` publishes formal decision records with a
# crash-injectable, directory-fsynced variant owned by the decision-record durability
# boundary. Folding it in requires the shared durability consolidation instead of this
# contract, so it stays the single documented exemption.
DURABILITY_EXEMPT = {SOURCE_ROOT / "recommendation/infra/persistence/decision_records.py"}


def _hard_link_lines(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "link"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "os"
    ]


def test_hard_link_publication_has_a_single_implementation() -> None:
    assert SEALING.is_file()
    assert _hard_link_lines(SEALING)

    offenders = {
        path.relative_to(ROOT).as_posix(): lines
        for path in sorted(SOURCE_ROOT.rglob("*.py"))
        if path != SEALING and path not in DURABILITY_EXEMPT and (lines := _hard_link_lines(path))
    }

    assert offenders == {}

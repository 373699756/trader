"""Immutable artifact publication must have exactly one implementation.

``os.link`` is what gives publication its create-or-conflict semantics: a path that
already exists is never overwritten. Every artifact family must reuse
``trader.infra.serialization.sealing`` so the temporary-file, flush and hard-link mechanics
stay reviewable in one place.
"""

import ast
from pathlib import Path

from tests.helpers.shared.python_source import source_tree

ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = ROOT / "src/trader"
SEALING = SOURCE_ROOT / "infra/serialization/sealing.py"

# ``infra/persistence/freeze_records.py`` publishes formal freeze records with a
# crash-injectable, directory-fsynced variant owned by the freeze-record durability
# boundary. Folding it in requires the shared durability consolidation instead of this
# contract, so it stays the single documented exemption.
DURABILITY_EXEMPT = {SOURCE_ROOT / "recommendation/infra/persistence/freeze_records.py"}
RAW_FILES = SOURCE_ROOT / "download/infra/company_raw_files.py"


def _hard_link_lines(tree: ast.AST) -> list[int]:
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
    assert _hard_link_lines(source_tree(SEALING))

    # Raw inputs share already verified bytes and OCR checkpoints. This is
    # reference-counted deduplication, not create-or-conflict report publication.
    raw_class, = (node for node in source_tree(RAW_FILES).body if isinstance(node, ast.ClassDef) and node.name == "CompanyRawFiles")
    complete, = (node for node in raw_class.body if isinstance(node, ast.FunctionDef) and node.name == "complete")
    deduplication_lines = _hard_link_lines(complete)

    offenders = {
        path.relative_to(ROOT).as_posix(): lines
        for path in sorted(SOURCE_ROOT.rglob("*.py"))
        if path != SEALING and path not in DURABILITY_EXEMPT
        and (lines := [line for line in _hard_link_lines(source_tree(path)) if path != RAW_FILES or line not in deduplication_lines])
    }

    assert offenders == {}

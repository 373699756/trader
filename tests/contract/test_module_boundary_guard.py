"""Counterexamples prove that architecture gates inspect active source boundaries."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.contract import test_architecture
from tests.helpers.module_boundaries import (
    BusinessSourceFiles,
    ImportCycles,
    LayerImportViolations,
    ParseSourceImports,
    SourceImportGraph,
    SourceImports,
    SourceImportTargets,
)


def _write_source(root: Path, relative: str, source: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def _business_sources(root: Path) -> None:
    for business in ("download", "training", "recommendation"):
        for layer in ("domain", "application"):
            _write_source(root, f"{business}/{layer}/values.py", "VALUE = 1\n")


@pytest.mark.parametrize("business", ("download", "training", "recommendation"))
@pytest.mark.parametrize("layer", ("domain", "application"))
@pytest.mark.parametrize(
    "source",
    (
        "import trader.BUSINESS.infra.adapter as implementation",
        "from trader.BUSINESS import infra as implementation",
        "from ..infra import adapter as implementation",
        "from .. import infra",
        "def execute():\n    from ..infra import adapter",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from ..infra import Adapter",
    ),
)
def test_active_dependency_gate_rejects_implementation_imports(tmp_path, monkeypatch, business, layer, source) -> None:
    root = tmp_path / "trader"
    _business_sources(root)
    _write_source(root, f"{business}/{layer}/poison.py", source.replace("BUSINESS", business))
    monkeypatch.setattr(test_architecture, "SOURCE_ROOT", root)

    with pytest.raises(AssertionError, match="poison.py"):
        test_architecture.test_active_dependency_direction()


@pytest.mark.parametrize("business", ("download", "training", "recommendation"))
@pytest.mark.parametrize("layer", ("domain", "application"))
def test_active_dependency_gate_rejects_other_business_implementations(business, layer) -> None:
    module = f"trader.{business}.{layer}.example"
    other = "training" if business != "training" else "download"
    source = f"from trader.{other}.infra.adapter import Adapter"

    assert LayerImportViolations(module, ParseSourceImports(source, module))


@pytest.mark.parametrize(
    "target",
    (
        "trader.training.application.tomorrow_training",
        "trader.training.domain.model_publication",
        "trader.download.application.read_published_history",
    ),
)
def test_recommendation_application_cannot_expand_the_publication_handoff(target) -> None:
    module = "trader.recommendation.application.example"
    source = f"from {target} import Value"

    assert LayerImportViolations(module, ParseSourceImports(source, module))


@pytest.mark.parametrize(
    "module",
    (
        "trader.recommendation.application.ports.loaded_profile",
        "trader.recommendation.application.pipeline.local_score.model_capability",
    ),
)
def test_existing_publication_value_handoff_remains_legal(module) -> None:
    source = "from trader.training.domain.model_publication import HeadTrainingStatus"

    assert LayerImportViolations(module, ParseSourceImports(source, module)) == ()


def test_core_cannot_hide_imports_behind_a_wildcard() -> None:
    module = "trader.training.domain.example"
    source = "from trader.infra.serialization.canonical_codec import *"

    assert LayerImportViolations(module, ParseSourceImports(source, module))


def test_active_dependency_gate_accepts_pure_business_values(tmp_path, monkeypatch) -> None:
    root = tmp_path / "trader"
    _business_sources(root)
    _write_source(
        root,
        "training/application/replay.py",
        "from trader.download.domain.published_history import PublishedHistory\n"
        "from trader.recommendation.domain.publication.models import Strategy\n",
    )
    monkeypatch.setattr(test_architecture, "SOURCE_ROOT", root)

    test_architecture.test_active_dependency_direction()


@pytest.mark.parametrize("layer", ("domain", "application"))
@pytest.mark.parametrize("empty_package", (False, True))
def test_business_scan_rejects_missing_or_empty_boundaries(tmp_path, layer, empty_package) -> None:
    root = tmp_path / "trader"
    if empty_package:
        for business in ("download", "training", "recommendation"):
            _write_source(root, f"{business}/{layer}/__init__.py", "")

    with pytest.raises(AssertionError, match=f"download/{layer}"):
        BusinessSourceFiles(root, layer)


@pytest.mark.parametrize("business", ("download", "training", "recommendation"))
@pytest.mark.parametrize(
    "source",
    (
        "import sqlite3 as driver\n",
        "from datetime import datetime as Moment\nVALUE = Moment.now()\n",
        "VALUE = open('local-file')\n",
        "from pathlib import Path\nVALUE = Path('local-file').read_text()\n",
    ),
)
def test_domain_io_gate_reads_each_active_domain(tmp_path, monkeypatch, business, source) -> None:
    root = tmp_path / "trader"
    _business_sources(root)
    _write_source(root, f"{business}/domain/poison.py", source)
    monkeypatch.setattr(test_architecture, "SOURCE_ROOT", root)

    with pytest.raises(AssertionError, match="poison.py"):
        test_architecture.test_domain_has_no_external_io_or_clock_reads()


@pytest.mark.parametrize("business", ("download", "training", "recommendation"))
@pytest.mark.parametrize("layer", ("domain", "application"))
@pytest.mark.parametrize(
    "source",
    (
        "import json\nVALUE = json.loads('{}')\n",
        "import json as codec\nVALUE = codec.loads('{}')\n",
        "from json import loads as Decode\nVALUE = Decode('{}')\n",
    ),
)
def test_json_decoder_gate_reads_each_active_core(tmp_path, monkeypatch, business, layer, source) -> None:
    root = tmp_path / "trader"
    _business_sources(root)
    _write_source(root, f"{business}/{layer}/poison.py", source)
    monkeypatch.setattr(test_architecture, "SOURCE_ROOT", root)

    with pytest.raises(AssertionError, match="JSON decoding"):
        test_architecture.test_domain_and_application_do_not_own_persistence_or_json_decoders()


@pytest.mark.parametrize("owner", ("http_api", "web"))
@pytest.mark.parametrize(
    "target",
    (
        "trader.bootstrap",
        "trader.download.application.download_history",
        "trader.training.infra.engine",
        "trader.recommendation.infra.persistence.decision_records",
        "trader.recommendation.application.pipeline.local_score.base_scoring",
    ),
)
def test_delivery_gate_rejects_business_execution_and_infrastructure(owner, target) -> None:
    module = f"trader.{owner}.example"
    assert LayerImportViolations(module, ParseSourceImports(f"import {target} as implementation", module))


@pytest.mark.parametrize("owner", ("http_api", "web"))
def test_delivery_gate_accepts_read_only_queries_and_domain_values(owner) -> None:
    module = f"trader.{owner}.example"
    source = (
        "from trader.recommendation.application.pipeline.freeze_publish.read_only_queries import UnifiedDecisionQueries\n"
        "from trader.recommendation.domain.publication.models import Strategy\n"
    )
    assert LayerImportViolations(module, ParseSourceImports(source, module)) == ()


def test_relative_imports_resolve_package_initializers_and_members(tmp_path) -> None:
    root = tmp_path / "trader"
    path = _write_source(root, "training/application/__init__.py", "from .. import infra as implementation\n")

    assert SourceImportTargets(path, root) == {"trader.training", "trader.training.infra"}
    assert SourceImports(path, root)[0].line == 1


def test_import_graph_detects_transitive_cycles_and_module_members(tmp_path) -> None:
    root = tmp_path / "trader"
    _write_source(root, "training/application/first.py", "from . import second\n")
    _write_source(root, "training/application/second.py", "from .third import Value\n")
    _write_source(root, "training/application/third.py", "import trader.training.application.first as first\n")

    cycles = ImportCycles(SourceImportGraph(root, import_time_only=True))

    assert cycles == (
        (
            "trader.training.application.first",
            "trader.training.application.second",
            "trader.training.application.third",
            "trader.training.application.first",
        ),
    )


def test_package_initializers_are_included_without_artificial_ancestor_cycles(tmp_path) -> None:
    root = tmp_path / "trader"
    _write_source(root, "training/__init__.py", "from . import first\n")
    _write_source(root, "training/first.py", "from .second import Value\n")
    _write_source(root, "training/second.py", "VALUE = 1\n")
    assert ImportCycles(SourceImportGraph(root, import_time_only=True)) == ()


def test_external_package_initializers_can_close_an_import_cycle(tmp_path) -> None:
    root = tmp_path / "trader"
    _write_source(root, "download/application/first.py", "from trader.training.domain.values import Value\n")
    _write_source(root, "training/__init__.py", "import trader.download.application.first\n")
    _write_source(root, "training/domain/values.py", "VALUE = 1\n")

    assert ImportCycles(SourceImportGraph(root, import_time_only=True))


@pytest.mark.parametrize(
    "source",
    (
        "from typing import TYPE_CHECKING as Checking\nif Checking:\n    from . import second\n",
        "import typing as hints\nif hints.TYPE_CHECKING:\n    from . import second\n",
        "from typing import TYPE_CHECKING\nif not TYPE_CHECKING:\n    pass\nelse:\n    from . import second\n",
    ),
)
def test_import_time_graph_excludes_type_only_cycles(tmp_path, source) -> None:
    root = tmp_path / "trader"
    _write_source(root, "training/application/first.py", source)
    _write_source(root, "training/application/second.py", "from . import first\n")

    assert ImportCycles(SourceImportGraph(root, import_time_only=True)) == ()


def test_import_time_graph_excludes_delayed_imports_but_dependency_graph_retains_them(tmp_path) -> None:
    root = tmp_path / "trader"
    _write_source(root, "training/application/first.py", "def execute():\n    from . import second\n")
    _write_source(root, "training/application/second.py", "from . import first\n")

    assert ImportCycles(SourceImportGraph(root, import_time_only=True)) == ()
    assert ImportCycles(SourceImportGraph(root))

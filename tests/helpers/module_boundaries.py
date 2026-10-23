"""Static import analysis for the active Trader business and delivery boundaries."""

from __future__ import annotations

import ast
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from tests.helpers.python_source import source_text

_BUSINESSES = frozenset({"download", "training", "recommendation"})
_SHARED_APPLICATION_MODULES = frozenset(
    {
        "trader.infra.shutdown",
        "trader.infra.workers",
        "trader.infra.cache_contracts",
        "trader.infra.serialization.canonical_codec",
    }
)
_SHARED_APPLICATION_SYMBOLS = {
    "trader.infra.workers": frozenset(
        {
            "WorkerExecutor",
            "ManagedWorkerExecutor",
            "WorkerResourceRejectedError",
            "SubmitOrReject",
            "SubmitWithBackpressure",
        }
    ),
    "trader.infra.shutdown": frozenset({"ShutdownDeadline", "ShutdownStep"}),
}
_READ_ONLY_MODULES = frozenset(
    {
        "trader.recommendation.application.pipeline.freeze_publish.read_only_queries",
        "trader.recommendation.application.pipeline.freeze_publish.event_stream",
    }
)


@dataclass(frozen=True)
class SourceImport:
    module: str
    member: str | None
    line: int
    deferred: bool = False
    type_checking: bool = False

    @property
    def targets(self) -> tuple[str, ...]:
        return (self.module, f"{self.module}.{self.member}") if self.member is not None else (self.module,)


class _ImportVisitor(ast.NodeVisitor):
    def __init__(self, tree: ast.Module, package: str) -> None:
        self.imports: list[SourceImport] = []
        self._package = package
        self._deferred = False
        self._type_checking = False
        self._typing_aliases = {"typing"}
        self._type_checking_aliases = {"TYPE_CHECKING"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self._typing_aliases.update(
                    alias.asname or alias.name for alias in node.names if alias.name == "typing"
                )
            elif isinstance(node, ast.ImportFrom) and node.module == "typing":
                self._type_checking_aliases.update(
                    alias.asname or alias.name for alias in node.names if alias.name == "TYPE_CHECKING"
                )

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.imports.append(SourceImport(alias.name, None, node.lineno, self._deferred, self._type_checking))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if node.level:
            package = self._package.split(".")
            if node.level > len(package):
                raise ValueError(f"relative import escapes package at line {node.lineno}")
            module = ".".join((*package[: len(package) - node.level + 1], *((module,) if module else ())))
        for alias in node.names:
            self.imports.append(SourceImport(module, alias.name, node.lineno, self._deferred, self._type_checking))

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        previous = self._deferred
        self._deferred = True
        self.generic_visit(node)
        self._deferred = previous

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_If(self, node: ast.If) -> None:
        test = node.test
        negated = isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
            test = test.operand
        type_guard = (
            isinstance(test, ast.Name)
            and test.id in self._type_checking_aliases
            or isinstance(test, ast.Attribute)
            and isinstance(test.value, ast.Name)
            and test.value.id in self._typing_aliases
            and test.attr == "TYPE_CHECKING"
        )
        previous = self._type_checking
        self._type_checking = previous or (type_guard and not negated)
        for child in node.body:
            self.visit(child)
        self._type_checking = previous or (type_guard and negated)
        for child in node.orelse:
            self.visit(child)
        self._type_checking = previous


def ParseSourceImports(source: str, module: str, *, is_package: bool = False) -> tuple[SourceImport, ...]:
    tree = ast.parse(source)
    visitor = _ImportVisitor(tree, module if is_package else module.rpartition(".")[0])
    visitor.visit(tree)
    return tuple(visitor.imports)


def SourceModuleName(path: Path, root: Path) -> str:
    parts = path.relative_to(root).with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join((root.name, *parts))


def SourceImports(path: Path, root: Path) -> tuple[SourceImport, ...]:
    return ParseSourceImports(source_text(path), SourceModuleName(path, root), is_package=path.name == "__init__.py")


def SourceImportTargets(path: Path, root: Path) -> frozenset[str]:
    return frozenset(target for imported in SourceImports(path, root) for target in imported.targets)


def BusinessSourceFiles(root: Path, layer: str) -> tuple[Path, ...]:
    files: list[Path] = []
    for business in sorted(_BUSINESSES):
        boundary = root / business / layer
        active = sorted(path for path in boundary.rglob("*.py") if path.name != "__init__.py")
        if not active:
            raise AssertionError(f"missing or empty active boundary: {business}/{layer}")
        files.extend(active)
    return tuple(files)


def LayerImportViolations(module: str, imports: tuple[SourceImport, ...]) -> tuple[SourceImport, ...]:
    return tuple(
        imported
        for imported in imports
        if imported.member == "*" or any(_forbidden_target(module, imported, target) for target in imported.targets)
    )


def _forbidden_target(module: str, imported: SourceImport, target: str) -> bool:
    owner = module.split(".")
    dependency = target.split(".")
    if len(owner) < 2:
        return False
    business, layer = owner[1], owner[2] if len(owner) > 2 else ""
    if business in _BUSINESSES and layer in {"domain", "application"} and dependency[0] == "flask":
        return True
    if len(dependency) < 2 or dependency[0] != "trader":
        return False
    target_owner, target_layer = dependency[1], dependency[2] if len(dependency) > 2 else ""
    delivery = {"http_api", "web", "entrypoints", "bootstrap"}
    if business in _BUSINESSES and layer in {"domain", "application"}:
        if target_owner in delivery:
            return True
        if business == "recommendation" and layer == "application" and target_owner in {"download", "training"}:
            # Existing immutable publication values are the only application
            # handoff from another business; implementation imports stay closed.
            return imported.module != "trader.training.domain.model_publication" or module not in {
                "trader.recommendation.application.ports.loaded_profile",
                "trader.recommendation.application.pipeline.local_score.model_capability",
            }
        if target_owner in _BUSINESSES:
            return target_layer in (
                {"application", "infra", "entrypoints"} if layer == "domain" else {"infra", "entrypoints"}
            )
        if target_owner == "infra":
            if layer == "domain":
                return imported.module != "trader.infra.serialization.canonical_codec"
            if business in {"download", "training"}:
                return imported.member not in _SHARED_APPLICATION_SYMBOLS.get(imported.module, frozenset())
            return imported.module not in _SHARED_APPLICATION_MODULES
    if business in _BUSINESSES and layer == "infra" or business == "infra":
        return target_owner in delivery or target_owner in _BUSINESSES and target_layer == "entrypoints"
    if business in {"http_api", "web"}:
        if target_owner in {"infra", "bootstrap", "entrypoints", "download", "training"}:
            return True
        if target_owner == "recommendation":
            return target_layer != "domain" and imported.module not in _READ_ONLY_MODULES
    return False


def SourceBoundaryViolations(root: Path) -> tuple[str, ...]:
    return tuple(
        f"{path.relative_to(root)}:{imported.line} -> {imported.targets[-1]}"
        for path in sorted(root.rglob("*.py"))
        for imported in LayerImportViolations(SourceModuleName(path, root), SourceImports(path, root))
    )


def CoreIoViolations(source: str, layer: str) -> tuple[str, ...]:
    tree = ast.parse(source)
    aliases: dict[str, str] = {}
    violations: list[str] = []
    io_modules = {"flask", "requests", "sqlite3", "subprocess", "socket", "urllib", "http", "aiohttp", "httpx"}
    for node in ast.walk(tree):
        imported: tuple[str, ...] = ()
        if isinstance(node, ast.Import):
            imported = tuple(alias.name for alias in node.names)
            aliases.update(
                (alias.asname or alias.name.split(".")[0], alias.name if alias.asname else alias.name.split(".")[0])
                for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom):
            imported = (node.module or "",)
            aliases.update((alias.asname or alias.name, f"{node.module}.{alias.name}") for alias in node.names)
        if layer == "domain" and isinstance(node, (ast.Import, ast.ImportFrom)):
            violations.extend(
                f"{node.lineno}: external I/O import {name}" for name in imported if name.split(".")[0] in io_modules
            )

    def qualified_name(node: ast.expr) -> str:
        if isinstance(node, ast.Name):
            return aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            return f"{qualified_name(node.value)}.{node.attr}"
        return ""

    file_operations = {
        "read_text",
        "read_bytes",
        "write_text",
        "write_bytes",
        "iterdir",
        "glob",
        "rglob",
        "mkdir",
        "unlink",
    }
    clock_reads = {"time.time", "time.monotonic", "time.perf_counter", "os.getenv", "os.environ.get"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = qualified_name(node.func)
        if name in {"json.load", "json.loads"}:
            violations.append(f"{node.lineno}: JSON decoding belongs at an explicit adapter")
        if layer == "domain" and (
            name in {"open", "builtins.open", *clock_reads}
            or isinstance(node.func, ast.Attribute)
            and node.func.attr in {*file_operations, "now", "today"}
        ):
            violations.append(f"{node.lineno}: domain file/configuration/clock access {name}")
    return tuple(violations)


def SourceImportGraph(root: Path, *, import_time_only: bool = False) -> dict[str, frozenset[str]]:
    modules = {SourceModuleName(path, root): path for path in sorted(root.rglob("*.py"))}
    graph: dict[str, frozenset[str]] = {}
    for module, path in modules.items():
        targets: set[str] = set()
        source_parts = module.split(".")
        initializing_packages = {".".join(source_parts[:index]) for index in range(1, len(source_parts))}
        for imported in SourceImports(path, root):
            if imported.type_checking or import_time_only and imported.deferred:
                continue
            for target in imported.targets:
                # Ancestors of this source are already initializing; other
                # package initializers are dependencies of a submodule import.
                parts = target.split(".")
                targets.add(target)
                targets.update(
                    prefix
                    for index in range(1, len(parts))
                    if (prefix := ".".join(parts[:index])) not in initializing_packages
                )
        graph[module] = frozenset(targets.intersection(modules) - {module})
    return graph


def ImportCycles(graph: Mapping[str, frozenset[str]]) -> tuple[tuple[str, ...], ...]:
    completed: set[str] = set()
    active: list[str] = []
    cycles: set[tuple[str, ...]] = set()

    def visit(module: str) -> None:
        if module in active:
            cycles.add((*active[active.index(module) :], module))
            return
        if module in completed:
            return
        active.append(module)
        for dependency in sorted(graph[module]):
            visit(dependency)
        active.pop()
        completed.add(module)

    for module in sorted(graph):
        visit(module)
    return tuple(sorted(cycles))


__all__ = [
    "BusinessSourceFiles",
    "CoreIoViolations",
    "ImportCycles",
    "LayerImportViolations",
    "ParseSourceImports",
    "SourceBoundaryViolations",
    "SourceImport",
    "SourceImportGraph",
    "SourceImportTargets",
    "SourceImports",
    "SourceModuleName",
]

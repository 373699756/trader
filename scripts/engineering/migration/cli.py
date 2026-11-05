"""Explicit inspection, candidate construction and rollback for isolated migration."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from scripts.core.contracts import ToolOutcome
from scripts.core.paths import PROJECT_ROOT

if TYPE_CHECKING:
    from trader.infra.sqlite_policy import SqliteIoPolicy


def _build_parser(action_identity: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    if action_identity == "migration":
        parser.add_argument("--source", type=Path, required=True, help="external candidate to inspect without writing")
        parser.add_argument("--config", type=Path, required=True)
    elif action_identity == "migration-build":
        parser.add_argument("--source", type=Path, required=True, help="external, stopped legacy workspace copy")
        parser.add_argument("--target", type=Path, required=True, help="new external candidate data directory")
        parser.add_argument("--default-profile", choices=("v2", "v3"), required=True)
        parser.add_argument("--runtime-config", type=Path, default=PROJECT_ROOT / "config/runtime.json")
    elif action_identity == "migration-rollback":
        parser.add_argument("--target", type=Path, required=True)
        parser.add_argument("--backup", type=Path, required=True)
    else:
        raise ValueError("unknown_migration_action")
    return parser


def _sqlite_policy(configuration: Path) -> SqliteIoPolicy:
    from trader.bootstrap import BuildSqlitePolicy
    from trader.infra.settings import LoadRuntimeSettings

    return BuildSqlitePolicy(LoadRuntimeSettings(configuration))


def Run(action_identity: str, arguments: list[str]) -> ToolOutcome:
    """Run only the selected action; parameter help never loads migration or configuration."""
    args = _build_parser(action_identity).parse_args(arguments)
    from scripts.engineering.migration import operations

    schema = "migration-inspection-v1" if action_identity == "migration" else "trader_data_layout_migration"
    result: dict[str, object]
    try:
        if action_identity == "migration":
            manifest = operations.inspect_layout(args.source, sqlite_policy=_sqlite_policy(args.config))
            result = {"schema_version": schema, "status": "passed", "summary": operations.ProjectManifest(manifest)}
        elif action_identity == "migration-build":
            manifest = operations.migrate(
                args.source,
                args.target,
                default_profile=args.default_profile,
                sqlite_policy=_sqlite_policy(args.runtime_config),
            )
            result = {"status": "passed", "state": "candidate_verified", **operations.ProjectManifest(manifest)}
        else:
            operations.rollback(args.target, args.backup)
            result = {"schema_version": schema, "status": "passed", "state": "rolled_back"}
    except (operations.MigrationError, OSError, sqlite3.Error, ValueError, KeyError, TypeError) as exc:
        return ToolOutcome(1, {
            "schema_version": schema,
            "status": "failed",
            "state": "failed",
            "error_code": _error_code(exc),
        })
    return ToolOutcome(0, result)


def _error_code(exc: BaseException) -> str:
    value = str(exc)
    return value if value.startswith("migration_") and value.isidentifier() and len(value) < 100 else "data_migration_failed"

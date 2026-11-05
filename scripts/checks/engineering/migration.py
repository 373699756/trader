"""Read-only inspection of an explicitly selected external migration candidate."""

import argparse
from pathlib import Path

from scripts.checks.reporting import emit_report


def Main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    from scripts.migrate_runtime_data import ProjectManifest, inspect_layout
    from trader.bootstrap import BuildSqlitePolicy
    from trader.infra.settings import LoadRuntimeSettings

    manifest = inspect_layout(args.source, sqlite_policy=BuildSqlitePolicy(LoadRuntimeSettings(args.config)))
    emit_report({"schema_version": "migration-inspection-v1", "status": "passed", "summary": ProjectManifest(manifest)})
    return 0

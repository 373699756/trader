"""Build a verified qfq layout in a separate directory; source data is never replaced."""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

from trader.download.infra.qfq_layout_migration import build_qfq_layout, inspect_legacy_qfq
from trader.download.infra.qfq_sqlite import qfq_shard_name


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="build and verify the explicit external target")
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--source-root", type=Path, help="legacy qfq root, containing v2 and v3")
    parser.add_argument("--target-root", type=Path, help="absent repository-external destination")
    args = parser.parse_args()
    source = args.source_root or args.project_root / "data/qfq"
    try:
        profiles = inspect_legacy_qfq(source)
        if args.apply:
            if args.target_root is None:
                raise ValueError("apply requires an explicit target")
            target = args.target_root.resolve()
            if target == args.project_root.resolve() or args.project_root.resolve() in target.parents:
                raise ValueError("migration target must be outside the repository")
            result = build_qfq_layout(
                source, args.target_root, args.project_root / "data/history/baostock/.maintenance.lock"
            )
            report = {"status": "built_verified", **asdict(result), "source_unchanged": True, "activated": False}
        else:
            report = {
                "status": "planned",
                "profiles": len(profiles),
                "codes": sum(len(profile.membership) for profile in profiles),
                "source_files": sum(len({name for _, name in p.membership}) for p in profiles),
                "target_files": sum(len({qfq_shard_name(code) for code, _ in p.membership}) for p in profiles),
                "activated": False,
            }
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print(json.dumps({"status": "failed", "reason": type(exc).__name__}))
        return 1
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

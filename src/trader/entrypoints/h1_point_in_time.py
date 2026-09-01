"""Explicit read-only command for H1 point-in-time coverage audits."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trader.domain.research.h1_point_in_time import H1PointInTimeSpec
from trader.infra.research.h1_point_in_time_archive import H1ArchiveConflictError, SQLiteH1PointInTimeArchive


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m trader.entrypoints.h1_point_in_time")
    parser.add_argument("--runtime-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    archive = SQLiteH1PointInTimeArchive(args.runtime_dir)
    audits: list[dict[str, object]] = []
    failed = False
    for strategy in ("today", "tomorrow", "d25"):
        spec = H1PointInTimeSpec(strategy)
        try:
            audit = archive.audit(spec)
        except H1ArchiveConflictError:
            failed = True
            audits.append(
                {
                    "strategy": strategy,
                    "state": "artifact_invalid",
                    "terminal_holdout_opened": False,
                    "production_authority": False,
                }
            )
            continue
        manifest = audit.manifest
        audits.append(
            {
                "strategy": audit.strategy,
                "state": manifest.state,
                "coverage_ratio": round(audit.coverage_ratio, 6),
                "universe_count": manifest.universe_count,
                "completed_codes": manifest.completed_codes,
                "common_trade_days": manifest.common_trade_days,
                "terminal_holdout_days": manifest.terminal_holdout_days,
                "manifest_hash": manifest.content_hash,
                "terminal_holdout_opened": audit.terminal_holdout_opened,
                "production_authority": False,
            }
        )
        failed = failed or manifest.state == "historical_data_insufficient"
    print(
        json.dumps(
            {"schema_version": "score_h1_point_in_time_audit_v1", "strategies": audits},
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]

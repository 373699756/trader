"""Explicit read-only command for H1 point-in-time coverage audits."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trader.bootstrap import BuildSqlitePolicy
from trader.infra.settings import LoadRuntimeSettings

from trader.training.domain.evaluation.h1_point_in_time import H1PointInTimeSpec
from trader.training.infra.research.h1_point_in_time_snapshot import (
    H1PointInTimeSnapshotConflictError,
    SQLiteH1PointInTimeSnapshot,
)


def Main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m trader.training.entrypoints.h1_point_in_time")
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=Path("config/runtime.json").resolve())
    args = parser.parse_args(argv)
    snapshot = SQLiteH1PointInTimeSnapshot(args.runtime_dir, sqlite_policy=BuildSqlitePolicy(LoadRuntimeSettings(args.config)))
    audits: list[dict[str, object]] = []
    failed = False
    for strategy in ("tomorrow", "d25"):
        spec = H1PointInTimeSpec(strategy)
        try:
            audit = snapshot.audit(spec)
        except H1PointInTimeSnapshotConflictError:
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
            {"schema_version": "score_h1_point_in_time_audit", "strategies": audits},
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(Main())


__all__ = ["Main"]

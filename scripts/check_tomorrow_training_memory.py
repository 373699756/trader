#!/usr/bin/env python3
"""Run one explicit Tomorrow training job and enforce its process peak-RSS budget."""

from __future__ import annotations

import argparse
import json
import resource
import sys
from pathlib import Path

from trader.application.research.tomorrow_training import TOMORROW_TRAINING_PEAK_RSS_MIB
from trader.infra.scoring.profiles.v3.training import run_tomorrow_training


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history-root", type=Path, required=True)
    parser.add_argument("--train-root", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--max-rss-mib", type=int, default=TOMORROW_TRAINING_PEAK_RSS_MIB)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    before = _peak_rss_bytes()
    result = run_tomorrow_training(
        args.history_root.resolve(),
        args.train_root.resolve(),
        source_commit=args.source_commit,
    )
    peak = _peak_rss_bytes()
    budget = args.max_rss_mib * 1024 * 1024
    payload = {
        "schema_version": "v3_training_memory_gate",
        "status": "passed" if result.status == "engineering_ready" and peak <= budget else "failed",
        "training_status": result.status,
        "training_input_hash": result.training_input_hash,
        "model_hash": result.model_hash,
        "report_hash": result.report_hash,
        "peak_rss_bytes": peak,
        "starting_peak_rss_bytes": before,
        "max_rss_bytes": budget,
        "failure_reasons": list(result.failure_reasons),
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload["status"] == "passed" else 1


def _peak_rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == "darwin" else value * 1024)


if __name__ == "__main__":
    raise SystemExit(main())

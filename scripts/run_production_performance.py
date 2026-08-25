"""Run the offline active-production performance gate and emit a reusable JSON report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trader.entrypoints.performance import run


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--baseline", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    report = run(args.config.resolve(), baseline_path=args.baseline.resolve() if args.baseline else None)
    payload = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)
    if args.output is not None:
        args.output.write_text(f"{payload}\n", encoding="utf-8")
    print(payload)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

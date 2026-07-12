from __future__ import annotations

import argparse
import json

from . import config
from .strategy_validation import StrategyValidationStore


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="审计 point-in-time 信号、执行标签与样本守恒")
    parser.add_argument("--db-path", default=config.VALIDATION_DB_PATH, help="验证数据库路径")
    parser.add_argument("--strategy", default="", help="仅审计指定策略")
    parser.add_argument("--sample-size", type=int, default=30, help="抽查已入选信号数量")
    args = parser.parse_args(argv)
    report = StrategyValidationStore(args.db_path).audit_point_in_time(
        strategy_name=args.strategy,
        sample_size=args.sample_size,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

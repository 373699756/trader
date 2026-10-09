"""Explicit one-time filename migration; inspect by default, use --apply to rename."""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from trader.download.infra.history_control_repository import HistoryMaintenanceLock
from trader.infra.atomic_files.json import atomic_write_json

_OLD_NAME = re.compile(r"range-([0-9]{5})-([0-9]{4})\.sqlite3")
_NEW_NAME = re.compile(r"[0-9]{5}(?:-[1-9][0-9]{0,3})?\.sqlite3")


@dataclass(frozen=True)
class RenamePlan:
    root: Path
    index: dict[str, str]
    renames: tuple[tuple[Path, Path], ...]


def _name(name: str) -> str:
    if match := _OLD_NAME.fullmatch(name):
        group, part = match.groups()
        return f"{group}.sqlite3" if int(part) == 0 else f"{group}-{int(part)}.sqlite3"
    if _NEW_NAME.fullmatch(name):
        return name
    raise ValueError("invalid qfq shard name")


def _plan(root: Path) -> RenamePlan:
    value: object = json.loads((root / "index.json").read_text(encoding="ascii"))
    if not isinstance(value, dict) or any(
        not isinstance(code, str)
        or len(code) != 6
        or not code.isascii()
        or not code.isdigit()
        or not isinstance(name, str)
        for code, name in value.items()
    ):
        raise ValueError("invalid qfq index")
    index = {code: _name(name) for code, name in value.items()}
    renames = tuple((path, root / _name(path.name)) for path in sorted(root.glob("range-*.sqlite3")))
    for source, target in renames:
        if source.is_symlink() or not source.is_file():
            raise ValueError("qfq source unavailable")
        if target.is_symlink() or (target.exists() and not os.path.samefile(source, target)):
            raise ValueError("qfq rename target conflict")
        for suffix in ("-journal", "-wal"):
            journal = Path(f"{source}{suffix}")
            if journal.exists() and journal.stat().st_size:
                if suffix == "-wal" or journal.read_bytes()[:8] != bytes(8):
                    raise ValueError("qfq source has pending writes")
    for code, name in value.items():
        if not (root / name).is_file() and not (root / index[code]).is_file():
            raise ValueError("qfq index references missing shard")
    return RenamePlan(root, index, renames)


def rename_qfq_shards(project_root: Path, *, apply: bool = False) -> tuple[int, int]:
    """Hard links keep the old routing valid until each new index is durable."""
    with HistoryMaintenanceLock(project_root / "data/history/baostock/.maintenance.lock"):
        plans = tuple(_plan(project_root / "data/qfq" / profile) for profile in ("v2", "v3"))
        for plan in plans:
            if apply:
                for source, target in plan.renames:
                    if not target.exists():
                        os.link(source, target)
                index_path = plan.root / "index.json"
                payload = json.dumps(plan.index, sort_keys=True, separators=(",", ":"))
                if index_path.read_text(encoding="ascii") != payload:
                    atomic_write_json(index_path, plan.index)
                for source, _target in plan.renames:
                    source.unlink()
                    journal = Path(f"{source}-journal")
                    if journal.exists() and journal.read_bytes()[:8] == bytes(8):
                        journal.unlink()
        return sum(len(plan.renames) for plan in plans), len(plans)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        files, indexes = rename_qfq_shards(args.project_root, apply=args.apply)
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"status": "failed", "reason": type(exc).__name__}))
        return 1
    print(json.dumps({"status": "applied" if args.apply else "planned", "files": files, "indexes": indexes}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

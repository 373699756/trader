"""Parse only the selected infra/disk action and validate its inputs."""

from __future__ import annotations

import argparse
import math
from collections.abc import Sequence
from pathlib import Path

from scripts.core.paths import PROJECT_ROOT
from scripts.infra.disk.options import DiskOptions


def BuildParser(
    action_identity: str, *, parents: Sequence[argparse.ArgumentParser] = ()
) -> argparse.ArgumentParser:
    if action_identity not in ("disk",):
        raise ValueError("unsupported infra/disk action")
    parser = argparse.ArgumentParser(description=__doc__, parents=list(parents), allow_abbrev=False)
    if not parents:
        parser.add_argument("--command-timeout-seconds", type=float, default=180.0)
    parser.set_defaults(
        action_identity=action_identity,
        command_timeout_seconds=180.0,
        runtime_config=PROJECT_ROOT / 'config/runtime.json',
        disk_paths=(),
        disk_policy_audit=False,
    )
    parser.add_argument('--runtime-config', default=str(PROJECT_ROOT / 'config/runtime.json'), help='runtime JSON configuration')
    parser.add_argument('--disk-path', type=Path, action='append', default=[], help='additional read-only disk probe path')
    parser.add_argument('--disk-policy-audit', action='store_true', help='audit SQLite policy consumers without opening business databases')
    return parser


def ParseOptions(args: argparse.Namespace) -> DiskOptions:
    options = DiskOptions(
        action_identity=args.action_identity,
        command_timeout_seconds=args.command_timeout_seconds,
        runtime_config=Path(args.runtime_config).expanduser().resolve(),
        disk_paths=tuple(path.expanduser().resolve() for path in args.disk_path),
        disk_policy_audit=args.disk_policy_audit,
    )
    if not math.isfinite(options.command_timeout_seconds) or not 0 < options.command_timeout_seconds <= 7200:
        raise ValueError("command timeout must be positive and must not exceed 7200 seconds")
    return options

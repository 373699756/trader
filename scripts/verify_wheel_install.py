#!/usr/bin/env python3
"""Install the built wheel outside the repository and verify public entrypoints and resources."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import venv
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path, default=PROJECT_ROOT / "dist")
    parser.add_argument(
        "--runtime-config",
        type=Path,
        default=PROJECT_ROOT / "config" / "v2" / "runtime.json",
    )
    return parser


def verify_wheel_install(*, dist_dir: Path, runtime_config: Path) -> dict[str, object]:
    wheels = tuple(sorted(dist_dir.resolve().glob("*.whl"), key=lambda path: path.stat().st_mtime_ns))
    if not wheels:
        raise FileNotFoundError("no wheel exists in the requested dist directory")
    wheel = wheels[-1]
    with tempfile.TemporaryDirectory(prefix="trader-wheel-install-") as temporary_name:
        temporary = Path(temporary_name)
        environment = temporary / "venv"
        venv.EnvBuilder(with_pip=True, system_site_packages=True).create(environment)
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        cli = environment / ("Scripts/trader-cli.exe" if os.name == "nt" else "bin/trader-cli")
        clean_environment = dict(os.environ)
        clean_environment.pop("PYTHONPATH", None)
        subprocess.run(
            (str(python), "-m", "pip", "install", "--disable-pip-version-check", str(wheel)),
            cwd=temporary,
            env=clean_environment,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            (str(cli), "--config", str(runtime_config.resolve()), "validate-config"),
            cwd=temporary,
            env=clean_environment,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            (str(python), "-m", "pip", "check"),
            cwd=temporary,
            env=clean_environment,
            check=True,
            capture_output=True,
            text=True,
        )
        probe = subprocess.run(
            (str(python), "-c", _RESOURCE_PROBE, str(PROJECT_ROOT)),
            cwd=temporary,
            env=clean_environment,
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(probe.stdout)
    if not isinstance(payload, dict):
        raise TypeError("wheel resource probe returned an invalid result")
    return {
        "schema_version": "trader_wheel_install_verification_v1",
        "status": "passed",
        "wheel": wheel.name,
        "resource_count": payload["resource_count"],
        "installed_outside_repository": payload["installed_outside_repository"],
    }


_RESOURCE_PROBE = r"""
import json
import sys
from importlib import resources
from pathlib import Path

import trader

repository = Path(sys.argv[1]).resolve()
installed = Path(trader.__file__).resolve()
required = (
    ("trader.web", "templates/index.html"),
    ("trader.web", "static/dashboard.css"),
    ("trader.web", "static/dashboard.js"),
    ("trader.web", "static/trader-mark.svg"),
    ("trader.resources.models", "tomorrow_v1_model.json"),
    ("trader.resources.models", "tomorrow_p2_model.json"),
)
for package, relative in required:
    resource = resources.files(package).joinpath(relative)
    if not resource.read_bytes():
        raise RuntimeError(f"wheel resource is empty: {package}/{relative}")
try:
    installed.relative_to(repository)
    outside = False
except ValueError:
    outside = True
if not outside:
    raise RuntimeError("wheel import resolved inside the repository")
print(json.dumps({"installed_outside_repository": outside, "resource_count": len(required)}, sort_keys=True))
"""


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = verify_wheel_install(dist_dir=args.dist_dir, runtime_config=args.runtime_config)
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""HTTP server process entry point."""

from __future__ import annotations

import argparse
import ipaddress
import os
from pathlib import Path

from trader.bootstrap import build_system
from trader.infra.process_lock import ProcessLock, ProcessLockError
from trader.infra.settings import RuntimeSettings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trader-server")
    parser.add_argument("--config", default=os.environ.get("TRADER_CONFIG", ""))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = _absolute_config_path(args.config)
    system = build_system(config_path)
    _validate_bind(system.settings)
    try:
        lock = ProcessLock(system.settings.runtime_dir / "server.lock")
        lock.acquire()
    except ProcessLockError as exc:
        raise SystemExit(str(exc)) from None
    with lock:
        system.start()
        try:
            system.app.run(
                host=system.settings.server.host,
                port=system.settings.server.port,
                debug=system.settings.server.debug,
                use_reloader=system.settings.server.use_reloader,
                threaded=True,
            )
        finally:
            system.stop()
    return 0


def _absolute_config_path(raw_path: str) -> Path:
    if not raw_path:
        raise SystemExit("--config or TRADER_CONFIG is required")
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        raise SystemExit("configuration path must be absolute")
    return path.resolve()


def _validate_bind(settings: RuntimeSettings) -> None:
    host = settings.server.host.strip().lower()
    is_loopback = host == "localhost"
    if not is_loopback:
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = False
    if not is_loopback and not settings.server.allow_insecure_non_loopback:
        raise SystemExit("non-loopback bind requires allow_insecure_non_loopback=true")


if __name__ == "__main__":
    raise SystemExit(main())

"""Server exposure guard for the local, unauthenticated dashboard."""

from __future__ import annotations

import ipaddress

from . import config


def is_loopback_bind_host(host: str) -> bool:
    normalized = host.strip().lower().strip("[]")
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def validate_server_bind(host: str) -> None:
    if is_loopback_bind_host(host):
        return
    if bool(getattr(config, "SERVER_ALLOW_INSECURE_NON_LOOPBACK", False)):
        return
    raise RuntimeError(
        "refusing unauthenticated non-loopback bind; keep HOST on localhost or set "
        "SERVER_ALLOW_INSECURE_NON_LOOPBACK=1 after accepting the security risk"
    )


__all__ = ["is_loopback_bind_host", "validate_server_bind"]

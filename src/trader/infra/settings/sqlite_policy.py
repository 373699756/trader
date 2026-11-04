"""Strict shared HDD/SSD disk policy parsing."""

from __future__ import annotations

from collections.abc import Mapping

from trader.infra.settings.models import SqliteSettings
from trader.infra.sqlite_policy import SqliteIoPolicy
from trader.infra.settings.parser import ConfigurationError, Integer, Number, RequireExactKeys, RequireMapping, Text


def ParseSqliteSettings(raw: Mapping[str, object]) -> SqliteSettings:
    RequireExactKeys(raw, {"hdd", "ssd"}, "sqlite_policy")
    return SqliteSettings(
        hdd=_parse_medium(RequireMapping(raw, "hdd"), "hdd"),
        ssd=_parse_medium(RequireMapping(raw, "ssd"), "ssd"),
    )


def _parse_medium(raw: Mapping[str, object], medium: str) -> SqliteIoPolicy:
    RequireExactKeys(
        raw,
        {"connection_timeout_seconds", "read_window_rows", "transaction_batch_rows", "checkpoint_mode"},
        f"sqlite_policy.{medium}",
    )
    checkpoint_mode = Text(raw, "checkpoint_mode")
    if checkpoint_mode not in {"PASSIVE", "RESTART", "TRUNCATE"}:
        raise ConfigurationError(f"sqlite_policy.{medium}.checkpoint_mode is invalid")
    return SqliteIoPolicy(
        connection_timeout_seconds=Number(raw, "connection_timeout_seconds", minimum=0.001, maximum=120.0),
        read_window_rows=Integer(raw, "read_window_rows", minimum=1),
        transaction_batch_rows=Integer(raw, "transaction_batch_rows", minimum=1),
        checkpoint_mode=checkpoint_mode,
    )


__all__ = ["ParseSqliteSettings"]

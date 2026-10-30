"""Strict shared HDD/SSD storage policy parsing."""

from __future__ import annotations

from collections.abc import Mapping

from trader.infra.settings.models import StorageMediumSettings, StorageSettings
from trader.infra.settings.parser import ConfigurationError, Integer, Number, RequireExactKeys, RequireMapping, Text


def ParseStorageSettings(raw: Mapping[str, object]) -> StorageSettings:
    RequireExactKeys(raw, {"hdd", "ssd"}, "storage_policy")
    return StorageSettings(
        hdd=_parse_medium(RequireMapping(raw, "hdd"), "hdd"),
        ssd=_parse_medium(RequireMapping(raw, "ssd"), "ssd"),
    )


def _parse_medium(raw: Mapping[str, object], medium: str) -> StorageMediumSettings:
    RequireExactKeys(
        raw,
        {"connection_timeout_seconds", "read_window_rows", "transaction_batch_rows", "checkpoint_mode"},
        f"storage_policy.{medium}",
    )
    checkpoint_mode = Text(raw, "checkpoint_mode")
    if checkpoint_mode not in {"PASSIVE", "RESTART", "TRUNCATE"}:
        raise ConfigurationError(f"storage_policy.{medium}.checkpoint_mode is invalid")
    return StorageMediumSettings(
        connection_timeout_seconds=Number(raw, "connection_timeout_seconds", minimum=0.001, maximum=120.0),
        read_window_rows=Integer(raw, "read_window_rows", minimum=1),
        transaction_batch_rows=Integer(raw, "transaction_batch_rows", minimum=1),
        checkpoint_mode=checkpoint_mode,
    )


__all__ = ["ParseStorageSettings"]

"""Current runtime configuration shared by isolated test resources."""

from functools import lru_cache
from pathlib import Path

from trader.infra.settings import LoadRuntimeSettings, RuntimeSettings
from trader.infra.sqlite_policy import SqliteIoPolicy

PROJECT_ROOT = Path(__file__).resolve().parents[3]


@lru_cache(maxsize=1)
def RuntimeSettingsFixture() -> RuntimeSettings:
    return LoadRuntimeSettings(PROJECT_ROOT / "config/runtime.json")


def SqlitePolicyFixture(settings: RuntimeSettings | None = None) -> SqliteIoPolicy:
    effective = settings if settings is not None else RuntimeSettingsFixture()
    return effective.sqlite_policy.hdd

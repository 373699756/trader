from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from zoneinfo import ZoneInfo
from trader.infra.cache_contracts import BuildCacheIdentity, CacheDatasetPolicy, CacheGroupPolicy, CacheIdentitySpec, CachePolicy

SHANGHAI = ZoneInfo("Asia/Shanghai")

NOW = datetime(2026, 7, 16, 10, 0, tzinfo=SHANGHAI)


class _CanonicalState(Enum):
    READY = "ready"


@dataclass(frozen=True)
class _CanonicalRecord:
    measured_at: datetime
    value: Decimal
    state: _CanonicalState
    tags: frozenset[str]


@dataclass
class MutableClocks:
    monotonic_value: float = 0.0
    wall_value: datetime = NOW

    def monotonic(self) -> float:
        return self.monotonic_value

    def wall(self) -> datetime:
        return self.wall_value

    def advance(self, seconds: float) -> None:
        self.monotonic_value += seconds
        self.wall_value += timedelta(seconds=seconds)


def _policy(*, capacity: int = 2, group_bytes: int = 100_000) -> CachePolicy:
    return CachePolicy(
        datasets={
            "full_market_quotes": CacheDatasetPolicy(
                refresh_ttl_seconds=None,
                action_max_age_seconds=None,
                cadence_task="full_market",
                action_max_age_multiplier=3.0,
                negative_ttl_seconds=10.0,
                capacity=capacity,
                group="realtime_observations",
                persisted=False,
            ),
            "daily_history": CacheDatasetPolicy(
                refresh_ttl_seconds=60.0,
                action_max_age_seconds=120.0,
                cadence_task=None,
                action_max_age_multiplier=None,
                negative_ttl_seconds=10.0,
                capacity=capacity,
                group="history_minutes_research",
                persisted=True,
            ),
        },
        groups={
            "realtime_observations": CacheGroupPolicy(max_bytes=group_bytes),
            "history_minutes_research": CacheGroupPolicy(max_bytes=group_bytes),
        },
        total_bytes=group_bytes * 2,
        runtime_reserve_bytes=1,
        pool_total_bytes=group_bytes * 2 + 1,
        estimator="canonical_json_utf8",
    )


def _identity(subject: str, *, dataset: str = "daily_history", phase: str = "morning_main"):
    return BuildCacheIdentity(
        CacheIdentitySpec(
            dataset=dataset,
            source="eastmoney",
            subject_key=subject,
            request={"codes": ["600002", "600001"], "fields": ["close"], "adjust": "qfq"},
            trade_date="2026-07-16",
            phase=phase,
            source_contract_version="eastmoney-contract",
            config_version="runtime-current",
            schema_version="market_snapshot",
        )
    )


def _capture_runtime_error(errors: list[str], function) -> None:
    try:
        function()
    except RuntimeError as exc:
        errors.append(str(exc))

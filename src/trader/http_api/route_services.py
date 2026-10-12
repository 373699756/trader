"""Injected read-only services used by Flask."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from trader.recommendation.application.pipeline.freeze_publish.event_stream import UnifiedDecisionEventStream
from trader.recommendation.application.pipeline.freeze_publish.read_only_queries import UnifiedDecisionQueries

StatusProvider = Callable[[], dict[str, object]]


@dataclass(frozen=True)
class WebApiConfig:
    heartbeat_seconds: float = 15.0
    snapshot_retention_seconds: float = 0.0


@dataclass(frozen=True)
class UnifiedWebServices:
    queries: UnifiedDecisionQueries
    events: UnifiedDecisionEventStream
    status_provider: StatusProvider
    config: WebApiConfig = WebApiConfig()
    profile_id: str = "v2"

    def __post_init__(self) -> None:
        if self.profile_id not in {"v2", "v3"}:
            raise ValueError("web scoring profile must be v2 or v3")


@dataclass(frozen=True)
class ProfiledWebServices:
    """Read-only profile lanes exposed by one HTTP process."""

    profiles: Mapping[str, UnifiedWebServices]
    default_profile: str
    config: WebApiConfig = WebApiConfig()
    long_profile: str = "v2"

    def __post_init__(self) -> None:
        profiles = dict(self.profiles)
        if (
            not profiles
            or self.default_profile not in profiles
            or self.long_profile not in profiles
            or set(profiles) - {"v2", "v3"}
        ):
            raise ValueError("profiled web services contain invalid profile identities")
        object.__setattr__(self, "profiles", MappingProxyType(profiles))

    def select(self, profile: str | None) -> tuple[str, UnifiedWebServices]:
        selected = profile or self.default_profile
        services = self.profiles.get(selected)
        if services is None:
            raise ValueError("scoring profile must be v2 or v3")
        return selected, services

    def select_long(self) -> tuple[str, UnifiedWebServices]:
        return self.long_profile, self.profiles[self.long_profile]


WebServices = UnifiedWebServices | ProfiledWebServices


__all__ = ["ProfiledWebServices", "StatusProvider", "UnifiedWebServices", "WebApiConfig", "WebServices"]

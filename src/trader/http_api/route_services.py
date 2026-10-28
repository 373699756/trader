"""Injected read-only services used by Flask."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from trader.recommendation.application.pipeline.freeze_publish.event_stream import UnifiedFreezeEventStream
from trader.recommendation.application.pipeline.freeze_publish.read_only_queries import UnifiedFreezeQueries
from trader.recommendation.domain.scoring.profile_identity import SCORING_PROFILE_IDS

StatusProvider = Callable[[], dict[str, object]]


@dataclass(frozen=True)
class WebApiConfig:
    heartbeat_seconds: float = 15.0
    snapshot_retention_seconds: float = 0.0


@dataclass(frozen=True)
class UnifiedWebServices:
    queries: UnifiedFreezeQueries
    events: UnifiedFreezeEventStream
    status_provider: StatusProvider
    config: WebApiConfig = WebApiConfig()
    profile_id: str = "v2"

    def __post_init__(self) -> None:
        if self.profile_id not in SCORING_PROFILE_IDS:
            raise ValueError("web scoring profile is invalid")


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
            or set(profiles) - set(SCORING_PROFILE_IDS)
        ):
            raise ValueError("profiled web services contain invalid profile identities")
        object.__setattr__(self, "profiles", MappingProxyType(profiles))

    def select(self, profile: str | None) -> tuple[str, UnifiedWebServices]:
        selected = profile or self.default_profile
        services = self.profiles.get(selected)
        if services is None:
            raise ValueError("scoring profile is not available in this process")
        return selected, services

    def select_long(self) -> tuple[str, UnifiedWebServices]:
        return self.long_profile, self.profiles[self.long_profile]


WebServices = UnifiedWebServices | ProfiledWebServices


__all__ = ["ProfiledWebServices", "StatusProvider", "UnifiedWebServices", "WebApiConfig", "WebServices"]

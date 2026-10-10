"""Owner-assigned revisions for accepted realtime inputs; no content serialization."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import replace
from typing import TypeAlias

from trader.recommendation.domain.market.models import FeatureSnapshot, MarketQuote

InputValue: TypeAlias = str | int | FeatureSnapshot | tuple["InputValue", ...]


class InputVersionClock:
    """Remember only the latest immutable value per bounded input kind."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._revision = 0
        self._latest: dict[str, tuple[InputValue, str]] = {}

    def accept(self, kind: str, value: InputValue) -> str:
        with self._lock:
            previous = self._latest.get(kind)
            if previous is not None and previous[0] == value:
                return previous[1]
            self._revision += 1
            version = f"{kind}:{self._revision}"
            self._latest[kind] = (value, version)
            return version

    def advance(self, kind: str) -> str:
        with self._lock:
            self._revision += 1
            return f"{kind}:{self._revision}"

    def features(self, kind: str, features: tuple[FeatureSnapshot, ...]) -> str:
        # Delivery timestamps alone do not mean the supplier facts changed.
        material = tuple(
            replace(
                feature,
                observed_at=feature.quote.source_time,
                quote=replace(feature.quote, received_time=feature.quote.source_time),
            )
            for feature in features
        )
        return self.accept(kind, material)


def quote_versions(features: tuple[FeatureSnapshot, ...]) -> dict[str, MarketQuote]:
    return {feature.quote.code: replace(feature.quote, received_time=feature.quote.source_time) for feature in features}


def changed_version_codes(previous: Mapping[str, MarketQuote], current: Mapping[str, MarketQuote]) -> tuple[str, ...]:
    return tuple(sorted(code for code in {*previous, *current} if previous.get(code) != current.get(code)))

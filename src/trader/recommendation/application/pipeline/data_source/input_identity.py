"""Owner-assigned revisions for accepted realtime inputs; no content serialization."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TypeAlias

from trader.recommendation.domain.market.models import FeatureSnapshot, MarketQuote

InputValue: TypeAlias = str | int | FeatureSnapshot | tuple["InputValue", ...]


@dataclass(frozen=True, slots=True, eq=False)
class _FeatureBatch:
    features: tuple[FeatureSnapshot, ...]

    def __eq__(self, other: object) -> bool:
        if isinstance(other, _FeatureBatch):
            return self.features is other.features or (
                len(self.features) == len(other.features)
                and all(_same_feature(left, right) for left, right in zip(self.features, other.features, strict=True))
            )
        # accept() may also receive an explicit material tuple for this kind.
        if isinstance(other, tuple):
            return len(other) == len(self.features) and all(
                isinstance(item, FeatureSnapshot)
                and item.observed_at == item.quote.received_time == item.quote.source_time
                and all(
                    evidence.published_at == evidence.received_at == item.quote.source_time
                    for evidence in item.evidence
                    if evidence.evidence_type == "structured_point_in_time"
                )
                and _same_feature(feature, item)
                for feature, item in zip(self.features, other, strict=True)
            )
        return NotImplemented


def _same_feature(left: FeatureSnapshot, right: FeatureSnapshot) -> bool:
    if left is right:
        return True
    if left.quote.source_time != right.quote.source_time or left.quote.data_version != right.quote.data_version:
        return False
    if left == right:
        return True
    if len(left.evidence) != len(right.evidence):
        return False
    # Compare every field, masking only observation times assigned by this
    # consumer. Real publications and corrected facts keep their own identity.
    evidence = tuple(
        replace(new, published_at=old.published_at, received_at=old.received_at)
        if old.evidence_type == new.evidence_type == "structured_point_in_time" else new
        for old, new in zip(left.evidence, right.evidence, strict=True)
    )
    return left == replace(
        right,
        observed_at=left.observed_at,
        quote=replace(right.quote, received_time=left.quote.received_time),
        evidence=evidence,
    )


class InputVersionClock:
    """Remember only the latest immutable value per bounded input kind."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._revision = 0
        self._latest: dict[str, tuple[InputValue | _FeatureBatch, str]] = {}

    def accept(self, kind: str, value: InputValue) -> str:
        return self._accept(kind, value)

    def _accept(self, kind: str, value: InputValue | _FeatureBatch) -> str:
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
        return self._accept(kind, _FeatureBatch(features))


def QuoteVersions(features: tuple[FeatureSnapshot, ...]) -> dict[str, MarketQuote]:
    return {feature.quote.code: replace(feature.quote, received_time=feature.quote.source_time) for feature in features}


def ChangedVersionCodes(previous: Mapping[str, MarketQuote], current: Mapping[str, MarketQuote]) -> tuple[str, ...]:
    return tuple(sorted(code for code in {*previous, *current} if previous.get(code) != current.get(code)))

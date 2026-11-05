"""Explicit process-local sample experiments; production never imports this module."""

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from threading import current_thread, main_thread
from types import MappingProxyType
from typing import Literal

from trader.recommendation.domain.market.feature_contracts import FeatureValue, QfqPriceAnchors
from trader.training.infra.sample_sequences import SampleSequenceLayout

ComputationCandidate = Literal["baseline", "indexed_anchors", "schema_cache", "combined"]


@dataclass(frozen=True)
class _IndexedPriceAnchors(QfqPriceAnchors):
    _prices: Mapping[int, float | None] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "_prices", MappingProxyType(dict(self.lagged_closes)))

    def lag(self, sessions: int) -> float | None:
        return self._prices.get(sessions)


@dataclass
class ComputationCandidateCounts:
    candidate: ComputationCandidate
    indexed_anchor_calls: int = 0
    schema_requests: int = 0
    schema_computations: int = 0


@contextmanager
def UseComputationCandidate(candidate: ComputationCandidate) -> Iterator[ComputationCandidateCounts]:
    """Install hooks only for one serial isolated worker, restoring even on cancellation."""
    from trader.training.infra import sample_builder, sample_index, sample_schema

    if candidate not in {"baseline", "indexed_anchors", "schema_cache", "combined"}:
        raise ValueError("training_computation_candidate_invalid")
    if current_thread() is not main_thread():
        raise ValueError("training_computation_candidate_requires_serial_worker")
    counts = ComputationCandidateCounts(candidate)
    calculate: Callable[[QfqPriceAnchors, tuple[int, ...]], tuple[FeatureValue, ...]] = sample_builder.CalculateProfileQfqAlpha
    schema_hash: Callable[[tuple[str, ...], SampleSequenceLayout | None], bytes] = sample_schema.SampleFeatureHash
    index_hash: Callable[[tuple[str, ...], SampleSequenceLayout | None], bytes] = sample_index.SampleFeatureHash

    def indexed(anchors: QfqPriceAnchors, horizons: tuple[int, ...]) -> tuple[FeatureValue, ...]:
        counts.indexed_anchor_calls += 1
        return calculate(_IndexedPriceAnchors(anchors.current_close, anchors.lagged_closes), horizons)

    @lru_cache(maxsize=64)
    def cached_schema(
        names: tuple[str, ...], layout: SampleSequenceLayout | None, _layout_types: tuple[type, ...]
    ) -> bytes:
        counts.schema_computations += 1
        return schema_hash(names, layout)

    def validated_schema(names: tuple[str, ...], layout: SampleSequenceLayout | None) -> bytes:
        counts.schema_requests += 1
        # Dataclass equality treats 1, 1.0 and True alike; the codec preserves their JSON types.
        layout_types = () if layout is None else tuple(type(value) for value in (layout.auxiliary, layout.steps, layout.channels))
        return cached_schema(names, layout, layout_types)

    try:
        if candidate in {"indexed_anchors", "combined"}:
            sample_builder.CalculateProfileQfqAlpha = indexed
        if candidate in {"schema_cache", "combined"}:
            sample_schema.SampleFeatureHash = validated_schema
            sample_index.SampleFeatureHash = validated_schema
        yield counts
    finally:
        sample_builder.CalculateProfileQfqAlpha = calculate
        sample_schema.SampleFeatureHash = schema_hash
        sample_index.SampleFeatureHash = index_hash
        cached_schema.cache_clear()

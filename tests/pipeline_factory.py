"""Test-only constructor adapter for explicit pipeline dependency collections."""

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime

from trader.application.cadence import CadencePolicy
from trader.application.pipeline import RecommendationPipeline
from trader.application.pipeline_dependencies import PipelineDependencies, PipelineOptions, PipelineResources
from trader.application.ports.market import MarketDataPorts, MarketSnapshotMetadata
from trader.application.ports.outcomes import OutcomeSettlementPort
from trader.application.ports.snapshots import SnapshotPorts
from trader.application.ports.types import JsonObject, freeze_json_object
from trader.application.publisher import SnapshotPublisher
from trader.application.recommendations import RecommendationEngine
from trader.application.status import RuntimeState
from trader.application.workers import BoundedExecutor


def build_pipeline(
    market_data,
    calendar,
    reviews,
    repository,
    event_audit,
    publisher: SnapshotPublisher,
    engine: RecommendationEngine,
    state: RuntimeState,
    *,
    config_version: str,
    candidate_pool_size: int,
    event_queue_size: int,
    priority_queue_size: int,
    now: Callable[[], datetime],
    market_workers: int = 6,
    normalization_workers: int = 2,
    strategy_workers: int = 3,
    deepseek_workers: int = 4,
    data_pool: BoundedExecutor | None = None,
    persistence_pool: BoundedExecutor | None = None,
    market_data_manages_workers: bool = False,
    cadence_policy: CadencePolicy | None = None,
    long_codes: Sequence[str] = (),
    long_target_prices: Mapping[str, float | None] | None = None,
    outcome_settlement: OutcomeSettlementPort | None = None,
) -> RecommendationPipeline:
    metadata = market_data if hasattr(market_data, "snapshot_metadata") else _MarketMetadataAdapter(market_data)
    observability = (
        repository
        if hasattr(repository, "record_data_source_health") and hasattr(repository, "observability_status")
        else _SnapshotObservabilityAdapter()
    )
    return RecommendationPipeline(
        PipelineDependencies(
            market=MarketDataPorts(
                full_market=market_data,
                candidates=market_data,
                quotes=market_data,
                research=market_data,
                references=market_data,
                metadata=metadata,
                outcomes=market_data,
            ),
            calendar=calendar,
            reviews=reviews,
            snapshots=SnapshotPorts(reader=repository, writer=repository, observability=observability),
            events=event_audit,
            publisher=publisher,
            engine=engine,
            state=state,
            now=now,
            outcome_settlement=outcome_settlement,
        ),
        PipelineOptions(
            config_version=config_version,
            candidate_pool_size=candidate_pool_size,
            event_queue_size=event_queue_size,
            priority_queue_size=priority_queue_size,
            market_workers=market_workers,
            normalization_workers=normalization_workers,
            strategy_workers=strategy_workers,
            deepseek_workers=deepseek_workers,
            market_data_manages_workers=market_data_manages_workers,
            cadence_policy=cadence_policy,
            long_codes=tuple(long_codes),
            long_target_prices=long_target_prices or {},
        ),
        PipelineResources(data_pool=data_pool, persistence_pool=persistence_pool),
    )


class _MarketMetadataAdapter:
    def __init__(self, market_data) -> None:
        self._market_data = market_data

    def health(self) -> JsonObject:
        provider = getattr(self._market_data, "health", None)
        return freeze_json_object(provider() if callable(provider) else {})

    def snapshot_metadata(self, codes: Sequence[str] | None = None) -> MarketSnapshotMetadata:
        return MarketSnapshotMetadata()


class _SnapshotObservabilityAdapter:
    def record_data_source_health(self, health: JsonObject, *, updated_at: datetime) -> None:
        return None

    def observability_status(self) -> JsonObject:
        return freeze_json_object({})

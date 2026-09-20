"""Scheduled refresh and typed projection for current model-industry references."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from trader.recommendation.application.runtime.source_lanes import SourceRequestSupersededError
from trader.recommendation.domain.market.models import ModelIndustryReference
from trader.infra.market_data.providers.baostock_industry import (
    BaoStockIndustryClient,
    BaoStockIndustryHealthStatus,
)
from trader.infra.market_data.service.gateway import MarketDataGateway
from trader.infra.market_data.service.market_cache_identity import _source_batch_identity
from trader.infra.market_data.service.market_task_runner import MarketTaskRunner
from trader.infra.market_data.service.observations import SourceObservation
from trader.infra.market_data.service.trading_calendar_state_codec import parse_date

_LOGGER = logging.getLogger(__name__)
_SOURCE_LANE = "baostock_reference"


@dataclass(frozen=True)
class ModelIndustryReferenceDependencies:
    gateway: MarketDataGateway
    runner: MarketTaskRunner
    client: BaoStockIndustryClient | None
    persistence_sink: Callable[[Sequence[SourceObservation]], None]


class ModelIndustryReferenceLoader:
    def __init__(
        self,
        dependencies: ModelIndustryReferenceDependencies,
        *,
        refresh_ttl_seconds: float,
        retry_seconds: float,
        monotonic: Callable[[], float],
    ) -> None:
        self._gateway = dependencies.gateway
        self._runner = dependencies.runner
        self._client = dependencies.client
        self._persistence_sink = dependencies.persistence_sink
        self._refresh_ttl_seconds = refresh_ttl_seconds
        self._retry_seconds = retry_seconds
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._refresh_inflight = False
        self._next_refresh_at = 0.0
        self._reference_rows = 0
        self._data_version = ""

    def schedule(self, observed_at: datetime, *, force: bool) -> None:
        if self._client is None:
            return
        with self._lock:
            now = self._monotonic()
            if self._refresh_inflight or (not force and now < self._next_refresh_at):
                return
            self._refresh_inflight = True
        lanes = self._runner.source_lanes
        if lanes is None or lanes.owns_current_thread(_SOURCE_LANE):
            succeeded = False
            try:
                self._refresh(observed_at)
                succeeded = True
            finally:
                self._complete(succeeded)
            return
        identity = _source_batch_identity("current_model_industry", (), observed_at, force=force)
        try:
            future = lanes.submit(_SOURCE_LANE, identity, observed_at, self._refresh, observed_at)
        except Exception:
            self._complete(False)
            raise
        future.add_done_callback(self._observe_refresh)

    def _refresh(self, observed_at: datetime) -> int:
        client = self._client
        if client is None:
            return 0
        observations = client.fetch(observed_at)
        self._gateway.update_reference_observations(observations)
        merged = self._gateway.reference_observations(tuple(item.subject_key for item in observations))
        self._persistence_sink(merged)
        latest = max(observations, key=lambda item: (item.source_time, item.data_version))
        with self._lock:
            self._reference_rows = len(observations)
            self._data_version = latest.data_version
        return len(observations)

    def _observe_refresh(self, future: Future[int]) -> None:
        succeeded = False
        try:
            future.result()
            succeeded = True
        except SourceRequestSupersededError:
            pass
        except Exception as exc:
            _LOGGER.warning("current model industry refresh failed: %s", type(exc).__name__)
        finally:
            self._complete(succeeded)

    def _complete(self, succeeded: bool) -> None:
        with self._lock:
            self._refresh_inflight = False
            self._next_refresh_at = self._monotonic() + (
                self._refresh_ttl_seconds if succeeded else self._retry_seconds
            )

    def recover(self, observations: Sequence[SourceObservation]) -> None:
        references = self._project(observations)
        with self._lock:
            self._reference_rows = len(references)
            self._data_version = max(
                (reference.data_version for reference in references.values()),
                default="",
            )

    def references(self, codes: Sequence[str]) -> Mapping[str, ModelIndustryReference]:
        return self._project(self._gateway.reference_observations(codes))

    def _project(
        self,
        observations: Sequence[SourceObservation],
    ) -> Mapping[str, ModelIndustryReference]:
        resolved: dict[str, ModelIndustryReference] = {}
        for observation in observations:
            fields = observation.fields
            industry = fields.get("model_industry")
            classification = fields.get("model_industry_classification")
            effective_date = fields.get("model_industry_effective_date")
            source = fields.get("model_industry_source")
            data_version = fields.get("model_industry_data_version")
            if not all(
                isinstance(value, str) for value in (industry, classification, effective_date, source, data_version)
            ):
                continue
            parsed_effective_date = parse_date(cast(str, effective_date))
            if parsed_effective_date is None:
                continue
            try:
                resolved[observation.subject_key] = ModelIndustryReference(
                    industry_id=cast(str, industry),
                    classification=cast(str, classification),
                    effective_date=parsed_effective_date,
                    source=cast(str, source),
                    data_version=cast(str, data_version),
                )
            except ValueError:
                continue
        return resolved

    def versions(self) -> Mapping[str, str]:
        with self._lock:
            return {"model_industry": self._data_version} if self._data_version else {}

    def health(self) -> BaoStockIndustryHealthStatus | None:
        return self._client.health() if self._client is not None else None

    def reference_rows(self) -> int:
        with self._lock:
            return self._reference_rows


__all__ = ["ModelIndustryReferenceDependencies", "ModelIndustryReferenceLoader"]

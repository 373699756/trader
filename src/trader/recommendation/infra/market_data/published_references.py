"""Fixed startup references; background observation reads only published identities."""

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType

from trader.download.domain.prepared_reference import PreparedReference
from trader.infra.market_data.observations import SourceObservation
from trader.infra.market_data.source_health import ModelIndustrySourceHealth, SecurityMasterSourceHealth
from trader.recommendation.application.ports.static_reference import StaticReferenceRead
from trader.recommendation.domain.market.eligibility import HistoricalStEligibilitySnapshot
from trader.recommendation.domain.market.models import Board, ModelIndustryReference
from trader.recommendation.domain.market.static import StaticIssuer, StaticMarketReference
from trader.recommendation.infra.market_data.market_cache_identity import _reference_epoch


@dataclass(frozen=True)
class ReferencePublicationStatus:
    loaded_identity: str | None
    disk_identity: str | None
    restart_required: bool
    failure: str | None


class PublishedReferences:
    def __init__(
        self,
        read: Callable[[], PreparedReference | None],
        read_identity: Callable[[], str | None],
        read_industries: Callable[[datetime], tuple[SourceObservation, ...]],
        *,
        max_age_seconds: float,
    ) -> None:
        self._read = read
        self._read_identity = read_identity
        self._read_industries = read_industries
        self._max_age = max_age_seconds
        self._lock = threading.RLock()
        self._initialization_lock = threading.Lock()
        self._reference: PreparedReference | None = None
        self._static: StaticMarketReference | None = None
        self._industries: Mapping[str, ModelIndustryReference] = MappingProxyType({})
        self._industry_source_time: datetime | None = None
        self._disk_identity: str | None = None
        self._failure: str | None = "reference_not_loaded"
        self._initialized = False

    def initialize(self) -> None:
        with self._initialization_lock:
            if self._initialized:
                return
            try:
                self._initialize()
            finally:
                self._initialized = True

    def _initialize(self) -> None:
        for _ in range(3):
            try:
                reference = self._read()
                if reference is None:
                    self._failure = "reference_unavailable"
                    return
                rows = self._read_industries(reference.checked_at) if reference.industry_version else ()
                if any(row.data_version != reference.industry_version for row in rows):
                    continue
                industries = {row.subject_key: _industry(row) for row in rows}
                if self._read_identity() != reference.identity:
                    continue
                static = StaticMarketReference(
                    tuple(
                        StaticIssuer(
                            item.code,
                            item.name,
                            Board(item.board),
                            "SSE" if item.source_code.startswith("sh") else "SZSE",
                            item.listed_on,
                            evidence.status == "ever_st" or "ST" in item.name.upper() or "退" in item.name,
                            False,
                        )
                        for item, evidence in zip(reference.official_universe, reference.st_evidence, strict=True)
                    ),
                    reference.identity,
                    reference.checked_at,
                )
            except (OSError, RuntimeError, ValueError, TypeError) as exc:
                self._failure = f"reference_{type(exc).__name__}"
                return
            with self._lock:
                self._reference, self._static = reference, static
                self._industries = MappingProxyType(industries)
                self._industry_source_time = min((row.source_time for row in rows), default=None)
                self._disk_identity = reference.identity
                self._failure = None
            return
        self._failure = "reference_publication_changed"

    def observe_publication(self) -> None:
        try:
            identity = self._read_identity()
        except (OSError, RuntimeError, ValueError, TypeError) as exc:
            with self._lock:
                self._failure = f"reference_publication_{type(exc).__name__}"
            return
        with self._lock:
            self._disk_identity = identity
            if self._reference is not None and self._failure and self._failure.startswith("reference_publication_"):
                self._failure = None

    def reference(self) -> PreparedReference | None:
        with self._lock:
            return self._reference

    def historical_st_eligibility(self) -> HistoricalStEligibilitySnapshot:
        reference = self.reference()
        codes = tuple(item.code for item in reference.eligible_universe) if reference else ()
        if reference is None or not codes:
            return HistoricalStEligibilitySnapshot.unavailable()
        return HistoricalStEligibilitySnapshot.ready(reference.identity, codes)

    def status(self) -> ReferencePublicationStatus:
        with self._lock:
            loaded = self._reference.identity if self._reference else None
            return ReferencePublicationStatus(loaded, self._disk_identity, loaded != self._disk_identity, self._failure)

    def model_industries(self, codes: Sequence[str]) -> Mapping[str, ModelIndustryReference]:
        with self._lock:
            return MappingProxyType({code: self._industries[code] for code in codes if code in self._industries})

    def versions(self) -> Mapping[str, str]:
        reference = self.reference()
        if reference is None:
            return MappingProxyType({})
        versions = {"security_master": reference.identity, "trading_calendar": reference.identity}
        if reference.industry_version:
            versions["model_industry"] = reference.industry_version
        return MappingProxyType(versions)

    def static_reference(self) -> StaticReferenceRead:
        with self._lock:
            return StaticReferenceRead(
                self._static, _reference_epoch(self.versions()), self._failure is not None, self._max_age
            )

    def security_observations(self) -> tuple[SourceObservation, ...]:
        reference = self.reference()
        if reference is None:
            return ()
        return tuple(
            SourceObservation(
                source="exchange_security_master",
                subject_key=item.code,
                observed_at=reference.checked_at,
                source_time=reference.checked_at,
                received_at=reference.checked_at,
                effective_at=reference.checked_at,
                data_version=reference.identity,
                fields={
                    "name": item.name,
                    "board": item.board,
                    "listing_date": item.listed_on.isoformat(),
                    "exchange": "SSE" if item.source_code.startswith("sh") else "SZSE",
                },
                missing_reasons={},
                payload_hash=item.content_hash,
                status="success",
                error_code=None,
            )
            for item in reference.official_universe
        )

    def security_master_health(self) -> SecurityMasterSourceHealth:
        reference = self.reference()
        rows = len(reference.official_universe) if reference else 0
        return SecurityMasterSourceHealth(
            True,
            0,
            0,
            0,
            0,
            0,
            None,
            None,
            None,
            self._failure,
            rows,
            rows,
            reference.checked_at if reference else None,
            0.0,
        )

    def model_industry_health(self) -> ModelIndustrySourceHealth:
        return ModelIndustrySourceHealth(
            snapshot_rows=len(self._industries),
            last_error=None if self._industries else "industry_unavailable",
            last_source_time=self._industry_source_time,
        )

    def model_industry_reference_rows(self) -> int:
        return len(self._industries)


def _industry(row: SourceObservation) -> ModelIndustryReference:
    fields = row.fields
    names = (
        "model_industry",
        "model_industry_classification",
        "model_industry_effective_date",
        "model_industry_source",
        "model_industry_data_version",
    )
    values = tuple(fields.get(name) for name in names)
    if not all(isinstance(value, str) and value for value in values):
        raise ValueError("industry_publication_invalid")
    return ModelIndustryReference(
        str(values[0]),
        str(values[1]),
        date.fromisoformat(str(values[2])),
        str(values[3]),
        str(values[4]),
    )


__all__ = ["PublishedReferences", "ReferencePublicationStatus"]

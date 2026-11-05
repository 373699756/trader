"""Current industry input from the validated local query database."""

from __future__ import annotations

from trader.infra.sqlite_policy import SqliteIoPolicy
from dataclasses import field

import json
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from trader.download.infra.baostock_industry import BaoStockIndustryClient
from trader.download.infra.industry_query_index import IndustryQuery, SQLiteIndustryQueryIndex
from trader.download.infra.industry_evidence_codec import IndustryEvidencePayload, ParseIndustryEvidence
from trader.infra.market_data.observations import SourceObservation
from trader.recommendation.domain.market.models import ModelIndustryReference

_CONTRACT = "baostock_dated_industry_queries"
_CURRENT_TABLE = "current_industry_query"
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_REFRESH_DAYS = 7
_COMPOSED_SOURCE = "reviewed_industry_publication"


@dataclass(frozen=True)
class CurrentIndustryPublication:
    path: Path
    client: BaoStockIndustryClient
    minimum_rows: int = 4_000

    sqlite_policy: SqliteIoPolicy = field(kw_only=True)

    def fetch(self, observed_at: datetime) -> tuple[SourceObservation, ...]:
        index = SQLiteIndustryQueryIndex(self.path, _CONTRACT, _CURRENT_TABLE, sqlite_policy=self.sqlite_policy)
        index.prepare()
        cache = CurrentIndustryCache(self.path, self.minimum_rows, sqlite_policy=self.sqlite_policy)
        query = cache.latest(observed_at)
        day = observed_at.astimezone(_SHANGHAI).date()
        supplier = _supplier_query(query) if query is not None else None
        refreshed = False
        if supplier is None or (day - supplier.day).days >= _REFRESH_DAYS:
            observations = self.client.fetch(observed_at)
            rows = [
                [item.subject_key, str(item.fields["model_industry_effective_date"]),
                 str(item.fields["model_industry"]), str(item.fields["model_industry_classification"])]
                for item in observations
            ]
            if len(rows) < self.minimum_rows:
                raise ValueError("industry_reference_snapshot_incomplete")
            supplier = index.build_query("baostock_current_industry", day, day, json.dumps(rows, ensure_ascii=False, separators=(",", ":")))
            refreshed = True
        known = {item.subject_key for item in _query_observations(supplier)}
        supplements = {}
        for item in index.read_evidence(observed_at):
            if item.code not in known:
                supplements[item.code] = item
        if not supplements and (query is None or query.source != _COMPOSED_SOURCE):
            if refreshed:
                index.save(supplier.source, supplier.day, supplier.observed_on, supplier.payload)
            return _query_observations(supplier)
        payload = json.dumps({
            "supplier": {
                "day": supplier.day.isoformat(), "source": supplier.source,
                "observed_on": supplier.observed_on.isoformat(), "payload": supplier.payload, "digest": supplier.digest,
            },
            "evidence": [IndustryEvidencePayload(item) for _, item in sorted(supplements.items())],
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        publication = index.build_query(_COMPOSED_SOURCE, day, day, payload)
        _query_observations(publication)
        index.save(_COMPOSED_SOURCE, day, day, payload)
        return cache.load(observed_at)



@dataclass(frozen=True)
class CurrentIndustryCache:
    path: Path
    minimum_rows: int = 4_000

    sqlite_policy: SqliteIoPolicy = field(kw_only=True)

    def load(self, observed_at: datetime) -> tuple[SourceObservation, ...]:
        query = self.latest(observed_at)
        return _query_observations(query) if query is not None else ()

    def latest(self, observed_at: datetime) -> IndustryQuery | None:
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("industry query time must be timezone-aware")
        return self._latest_valid_query(SQLiteIndustryQueryIndex(self.path, _CONTRACT, _CURRENT_TABLE, sqlite_policy=self.sqlite_policy), observed_at)

    def _latest_valid_query(self, cache: SQLiteIndustryQueryIndex, observed_at: datetime) -> IndustryQuery | None:
        cursor = observed_at.astimezone(_SHANGHAI).date()
        for _ in range(64):
            query = cache.latest(cursor)
            if query is None:
                return None
            try:
                observations = _query_observations(query, as_of=observed_at)
            except (TypeError, ValueError):
                observations = ()
            if len(observations) >= self.minimum_rows:
                return query
            if query.day == date.min:
                return None
            cursor = query.day - timedelta(days=1)
        return None



def _query_observations(query: IndustryQuery, *, as_of: datetime | None = None) -> tuple[SourceObservation, ...]:
    if query.source == _COMPOSED_SOURCE:
        return _composed_observations(query, as_of=as_of)
    values = json.loads(query.payload)
    if not isinstance(values, list) or len(values) > 20_000:
        raise ValueError("industry query payload is invalid")
    source_time = datetime.combine(query.observed_on, time.min, _SHANGHAI)
    version = "industry-query:" + query.digest
    result: list[SourceObservation] = []
    codes: set[str] = set()
    for row in values:
        if not isinstance(row, list) or len(row) != 4 or not all(isinstance(item, str) for item in row):
            raise ValueError("industry query row is invalid")
        code, effective_raw, industry, classification = row
        effective = date.fromisoformat(effective_raw)
        if len(code) != 6 or not code.isdigit() or code in codes or effective > query.day:
            raise ValueError("industry query identity is invalid")
        ModelIndustryReference(industry, classification, effective, "baostock", version)
        codes.add(code)
        result.append(
            SourceObservation(
                source="baostock_industry",
                subject_key=code,
                observed_at=source_time,
                source_time=source_time,
                received_at=source_time,
                effective_at=datetime.combine(effective, time.min, _SHANGHAI),
                data_version=version,
                fields={
                    "model_industry": industry,
                    "model_industry_classification": classification,
                    "model_industry_effective_date": effective_raw,
                    "model_industry_source": "baostock",
                    "model_industry_data_version": version,
                },
                missing_reasons={},
                payload_hash="",
                status="success",
                error_code=None,
            )
        )
    return tuple(result)


def _supplier_query(query: IndustryQuery) -> IndustryQuery:
    if query.source != _COMPOSED_SOURCE:
        return query
    raw = json.loads(query.payload)
    if not isinstance(raw, dict) or set(raw) != {"supplier", "evidence"}:
        raise ValueError("industry_publication_invalid")
    supplier = raw["supplier"]
    if (
        not isinstance(supplier, dict) or set(supplier) != {"day", "source", "observed_on", "payload", "digest"}
        or not all(isinstance(value, str) for value in supplier.values())
        or supplier["source"] == _COMPOSED_SOURCE
    ):
        raise ValueError("industry_publication_supplier_invalid")
    result = IndustryQuery(
        date.fromisoformat(supplier["day"]), supplier["source"], date.fromisoformat(supplier["observed_on"]),
        supplier["payload"], supplier["digest"],
    )
    if not result.day <= result.observed_on <= query.observed_on:
        raise ValueError("industry_publication_supplier_future")
    return result


def _composed_observations(query: IndustryQuery, *, as_of: datetime | None = None) -> tuple[SourceObservation, ...]:
    raw = json.loads(query.payload)
    supplier = _supplier_query(query)
    values = raw["evidence"]
    if not isinstance(values, list) or len(values) > 1000:
        raise ValueError("industry_publication_evidence_invalid")
    records = tuple(ParseIndustryEvidence(value) for value in values)
    version = "industry-query:" + query.digest
    result = list(_query_observations(supplier))
    codes = {item.subject_key for item in result}
    result = [
        replace(item, data_version=version, fields={**item.fields, "model_industry_data_version": version})
        for item in result
    ]
    for item in records:
        if item.code in codes or not item.applies(query.day) or item.received_at.date() > query.observed_on:
            raise ValueError("industry_publication_evidence_conflict")
        if as_of is not None and (not item.applies(as_of.astimezone(_SHANGHAI).date()) or item.received_at > as_of):
            raise ValueError("industry_publication_evidence_outside_observation")
        codes.add(item.code)
        result.append(SourceObservation(
            source="official_disclosure", subject_key=item.code, observed_at=item.received_at,
            source_time=item.disclosed_at, received_at=item.received_at,
            effective_at=datetime.combine(item.effective_from, time.min, _SHANGHAI),
            data_version=version,
            fields={
                "model_industry": item.mapping.target_industry, "model_industry_classification": "证监会行业分类",
                "model_industry_effective_date": item.effective_from.isoformat(),
                "model_industry_source": "official_disclosure", "model_industry_data_version": version,
                "industry_evidence_id": item.identity, "industry_evidence_url": item.source_url,
                "industry_native_classification": item.mapping.native_classification,
                "industry_native_code": item.mapping.native_code,
            }, missing_reasons={}, payload_hash=item.document_sha256, status="success", error_code=None,
        ))
    return tuple(sorted(result, key=lambda item: item.subject_key))


__all__ = ["CurrentIndustryCache", "CurrentIndustryPublication"]

"""Publish current CAPCO classifications from bounded, cached CNINFO evidence."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from trader.download.application.progress_message import ProgressField, ProgressLevel, ProgressMessage
from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.application.prepare_reference import IndustryDatabaseIncompatibleError
from trader.recommendation.domain.market.models import ModelIndustryReference
from trader.download.infra.cninfo_industry import (
    CninfoIndustryChange,
    CninfoIndustryClient,
    ParseCninfoIndustryChanges,
    ParseCninfoIndustryTaxonomy,
    ResolveCninfoIndustryCategory,
)
from trader.download.infra.industry_query_index import IndustryQuery, IndustrySourceDocument, SQLiteIndustryQueryIndex
from trader.infra.http_contracts import HttpCancelled
from trader.infra.market_data.observations import JsonScalar, SourceObservation
from trader.infra.sqlite_policy import SqliteIoPolicy

_CONTRACT = "cninfo_industry_queries"
_SOURCE = "cninfo_industry"
_STANDARD = "中国上市公司协会上市公司行业分类标准"
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_REFRESH_AGE = timedelta(days=7)
_TAXONOMY_KEY = "cninfo:p_public0002:008001"
_MAX_ROWS = 12_000


class CurrentIndustryDatabaseIncompatibleError(IndustryDatabaseIncompatibleError):
    """The industry download must be recreated using the current source contract."""


def _now() -> datetime:
    return datetime.now(_SHANGHAI)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("current_industry_time_invalid")
    return value.astimezone(_SHANGHAI)


@dataclass(frozen=True)
class CurrentIndustryPublication:
    path: Path
    client: CninfoIndustryClient
    minimum_rows: int = 1
    sqlite_policy: SqliteIoPolicy = field(kw_only=True)
    cancel_requested: Callable[[], bool] = field(default=lambda: False, kw_only=True)
    clock: Callable[[], datetime] = field(default=_now, kw_only=True)
    report: Callable[[ProgressMessage], None] | None = field(default=None, kw_only=True)

    def _check(self) -> None:
        if self.cancel_requested():
            raise HttpCancelled("cninfo_industry_cancelled")

    def _progress(self, code: str, completed: int, total: int, *, cached: bool, missing: bool = False) -> None:
        if self.report is not None:
            self.report(ProgressMessage((
                ProgressField("当前行业"), ProgressField(f"{completed}/{total}股"),
                ProgressField(f"代码 {code}"), ProgressField("缓存复用" if cached else "接口获取"),
                ProgressField("分类缺失" if missing else "完成", ProgressLevel.WARNING if missing else ProgressLevel.SUCCESS),
            )))

    def fetch(
        self,
        observed_at: datetime,
        universe: tuple[BaoStockSecurity, ...],
        calendar: tuple[date, ...],
    ) -> tuple[SourceObservation, ...]:
        observed_at = _aware(observed_at)
        day = observed_at.date()
        codes = tuple(item.code for item in universe)
        if (
            not codes or len(codes) > _MAX_ROWS or codes != tuple(sorted(set(codes)))
            or not calendar or len(calendar) > 20_000 or calendar != tuple(sorted(set(calendar)))
            or calendar[0] > day
        ):
            raise ValueError("current_industry_population_invalid")
        self._check()
        index = SQLiteIndustryQueryIndex(self.path, _CONTRACT, "current_industry_query", sqlite_policy=self.sqlite_policy)
        index.prepare()
        cut = max(observed_at, _aware(self.clock()))
        cache = CurrentIndustryCache(self.path, self.minimum_rows, sqlite_policy=self.sqlite_policy)
        previous = cache.latest(cut)
        if previous is not None:
            payload = _payload(previous)
            if (
                payload["universe"] == list(codes)
                and date.fromisoformat(_text(payload, "requested_from")) <= calendar[0]
                and date.fromisoformat(_text(payload, "requested_to")) <= day
                and cut < _timestamp(payload, "refresh_after")
            ):
                if self.report is not None:
                    self.report(ProgressMessage((ProgressField("当前行业"), ProgressField(f"当前发布缓存复用 {len(codes)} 股"))))
                return _query_observations(previous, as_of=cut)
        taxonomy_document = index.read_source_document(_TAXONOMY_KEY, cut)
        if taxonomy_document is not None and taxonomy_document.requested_to > day:
            raise ValueError("current_industry_source_window_in_future")
        if taxonomy_document is None or cut - taxonomy_document.received_at >= _REFRESH_AGE:
            taxonomy = self.client.fetch_taxonomy(cancel_requested=self.cancel_requested)
            self._check()
            start = min(calendar[0], taxonomy_document.requested_from) if taxonomy_document is not None else calendar[0]
            taxonomy_document = IndustrySourceDocument(_TAXONOMY_KEY, start, day, _aware(self.clock()), taxonomy.response_content)
            index.save_source_document(taxonomy_document)
        else:
            taxonomy = ParseCninfoIndustryTaxonomy(taxonomy_document.content)
        rows: list[dict[str, object]] = []
        gaps: list[dict[str, str]] = []
        documents = [taxonomy_document]
        for completed, code in enumerate(codes, 1):
            self._check()
            key = "cninfo:p_stock2110:" + code
            cut = max(cut, _aware(self.clock()))
            document = index.read_source_document(key, cut)
            if document is not None and document.requested_to > day:
                raise ValueError("current_industry_source_window_in_future")
            cached = document is not None and document.requested_from <= calendar[0] and cut - document.received_at < _REFRESH_AGE
            if document is None or not cached:
                start = min(calendar[0], document.requested_from) if document is not None else calendar[0]
                changes = self.client.fetch_changes(code, start, day, cancel_requested=self.cancel_requested)
                self._check()
                document = IndustrySourceDocument(key, start, day, _aware(self.clock()), changes.response_content)
                index.save_source_document(document)
            else:
                changes = ParseCninfoIndustryChanges(document.content, code, document.requested_from, document.requested_to)
            documents.append(document)
            supported = tuple(item for item in changes.rows if item.is_current_standard and item.is_latest and item.changed_on <= day)
            if not supported:
                reason = "current_latest_record_absent" if any(item.is_current_standard for item in changes.rows) else "current_standard_absent"
                gaps.append({"code": code, "reason": reason if changes.rows else "industry_records_empty"})
                self._progress(code, completed, len(codes), cached=cached, missing=True)
                continue
            change = max(supported, key=lambda item: item.changed_on)
            category = ResolveCninfoIndustryCategory(taxonomy, change)
            if category is None:
                raise ValueError("current_industry_category_unavailable")
            rows.append(_row_payload(change, category.code, category.name, document))
            self._progress(code, completed, len(codes), cached=cached)
        self._check()
        if len(rows) < self.minimum_rows:
            raise ValueError("industry_reference_snapshot_incomplete")
        completed_at = max(observed_at, _aware(self.clock()), *(document.received_at for document in documents))
        payload = {
            "universe": list(codes),
            "requested_from": max(document.requested_from for document in documents[1:]).isoformat(),
            "requested_to": min(document.requested_to for document in documents[1:]).isoformat(),
            "received_at": completed_at.isoformat(),
            "refresh_after": (min(document.received_at for document in documents) + _REFRESH_AGE).isoformat(),
            "taxonomy_sha256": taxonomy.response_sha256,
            "taxonomy_received_at": taxonomy_document.received_at.isoformat(),
            "rows": rows,
            "gaps": gaps,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        query = index.build_query(_SOURCE, day, completed_at.date(), encoded)
        result = _query_observations(query, as_of=max(completed_at, _aware(self.clock())))
        index.save(_SOURCE, day, completed_at.date(), encoded)
        return result


def _row_payload(change: CninfoIndustryChange, category_code: str, category_name: str, document: IndustrySourceDocument) -> dict[str, object]:
    return {
        "code": change.code, "category_code": category_code, "category_name": category_name,
        "industry_code": change.industry_code, "standard_code": change.standard_code, "standard_name": change.standard_name,
        "section_name": change.section_name, "subcategory_name": change.subcategory_name,
        "category_native_name": change.category_name, "middle_name": change.middle_name,
        "changed_on": change.changed_on.isoformat(), "latest_marker": change.latest_marker,
        "response_sha256": hashlib.sha256(document.content).hexdigest(),
        "received_at": document.received_at.isoformat(),
        "requested_from": document.requested_from.isoformat(), "requested_to": document.requested_to.isoformat(),
    }


@dataclass(frozen=True)
class CurrentIndustryCache:
    path: Path
    minimum_rows: int = 1
    sqlite_policy: SqliteIoPolicy = field(kw_only=True)

    def load(self, observed_at: datetime) -> tuple[SourceObservation, ...]:
        query = self.latest(observed_at)
        return _query_observations(query, as_of=observed_at) if query is not None else ()

    def latest(self, observed_at: datetime) -> IndustryQuery | None:
        observed_at = _aware(observed_at)
        index = SQLiteIndustryQueryIndex(self.path, _CONTRACT, "current_industry_query", sqlite_policy=self.sqlite_policy)
        cursor = observed_at.date()
        for _ in range(64):
            query = index.latest(cursor)
            if query is None:
                return None
            try:
                observations = _query_observations(query, as_of=observed_at)
            except CurrentIndustryDatabaseIncompatibleError:
                raise
            except (TypeError, ValueError):
                observations = ()
            if len(observations) >= self.minimum_rows:
                return query
            if query.day == date.min:
                return None
            cursor = query.day - timedelta(days=1)
        return None


def _text(payload: dict[str, object], name: str) -> str:
    value = payload[name]
    if not isinstance(value, str) or not value.strip():
        raise ValueError("current_industry_payload_invalid")
    return value


def _timestamp(payload: dict[str, object], name: str) -> datetime:
    return _aware(datetime.fromisoformat(_text(payload, name)))


def _payload(query: IndustryQuery) -> dict[str, object]:
    payload: object = json.loads(query.payload)
    if (
        query.source != _SOURCE or not isinstance(payload, dict)
        or set(payload) != {"universe", "requested_from", "requested_to", "received_at", "refresh_after", "taxonomy_sha256", "taxonomy_received_at", "rows", "gaps"}
    ):
        raise CurrentIndustryDatabaseIncompatibleError("current_industry_payload_incompatible")
    return payload


def _query_observations(query: IndustryQuery, *, as_of: datetime) -> tuple[SourceObservation, ...]:
    payload = _payload(query)
    as_of = _aware(as_of)
    rows, gaps, universe = payload["rows"], payload["gaps"], payload["universe"]
    if (
        not isinstance(universe, list) or not universe or len(universe) > _MAX_ROWS
        or not all(isinstance(code, str) and re.fullmatch(r"[0-9]{6}", code) for code in universe)
        or universe != sorted(set(universe)) or not isinstance(rows, list) or len(rows) > _MAX_ROWS
        or not isinstance(gaps, list) or len(gaps) > _MAX_ROWS
        or _timestamp(payload, "received_at") > as_of or _timestamp(payload, "taxonomy_received_at") > as_of
        or _timestamp(payload, "received_at").date() != query.observed_on
        or _timestamp(payload, "taxonomy_received_at") > _timestamp(payload, "received_at")
        or _timestamp(payload, "refresh_after") > _timestamp(payload, "received_at") + _REFRESH_AGE
        or re.fullmatch(r"[0-9a-f]{64}", _text(payload, "taxonomy_sha256")) is None
        or date.fromisoformat(_text(payload, "requested_from")) > date.fromisoformat(_text(payload, "requested_to"))
        or date.fromisoformat(_text(payload, "requested_to")) > query.day
    ):
        raise ValueError("current_industry_payload_invalid")
    codes: set[str] = set()
    result: list[SourceObservation] = []
    version = "industry-query:" + query.digest
    completed_at = _timestamp(payload, "received_at")
    expected = {"code", "category_code", "category_name", "industry_code", "standard_code", "standard_name", "section_name", "subcategory_name", "category_native_name", "middle_name", "changed_on", "latest_marker", "response_sha256", "received_at", "requested_from", "requested_to"}
    for row in rows:
        if not isinstance(row, dict) or set(row) != expected:
            raise ValueError("current_industry_row_invalid")
        code = _text(row, "code")
        changed_on = date.fromisoformat(_text(row, "changed_on"))
        received_at = _timestamp(row, "received_at")
        native_code, native_name = _text(row, "category_code"), _text(row, "category_name")
        marker = row["latest_marker"]
        if (
            code not in universe or code in codes or changed_on > query.day or received_at > as_of or received_at > completed_at
            or _text(row, "standard_code") != "008001" or _text(row, "standard_name") != _STANDARD
            or re.fullmatch(r"[A-S][0-9]{2}", native_code) is None
            or type(marker) not in (str, int) or marker not in (1, "1")
            or re.fullmatch(r"[0-9a-f]{64}", _text(row, "response_sha256")) is None
            or date.fromisoformat(_text(row, "requested_from")) > changed_on
            or date.fromisoformat(_text(row, "requested_to")) < changed_on
            or date.fromisoformat(_text(row, "requested_to")) > query.day
        ):
            raise ValueError("current_industry_row_invalid")
        industry = "capco:" + native_code + native_name
        reference = ModelIndustryReference(industry, _STANDARD, changed_on, _SOURCE, version)
        fields: dict[str, JsonScalar] = {
            "model_industry": reference.industry_id, "model_industry_classification": reference.classification,
            "model_industry_effective_date": reference.effective_date.isoformat(), "model_industry_source": reference.source,
            "model_industry_data_version": reference.data_version, "industry_native_code": _text(row, "industry_code"),
            "industry_standard_code": _text(row, "standard_code"), "industry_latest_marker": str(marker),
            "industry_source_requested_from": _text(row, "requested_from"),
            "industry_source_requested_to": _text(row, "requested_to"),
            "industry_record_received_at": received_at.isoformat(),
            "industry_taxonomy_received_at": _text(payload, "taxonomy_received_at"),
        }
        for name in ("section_name", "subcategory_name", "category_native_name", "middle_name"):
            value = row[name]
            if value is not None and not isinstance(value, str):
                raise ValueError("current_industry_row_invalid")
            fields["industry_" + name] = value
        codes.add(code)
        result.append(SourceObservation(
            source=_SOURCE, subject_key=code, observed_at=completed_at, source_time=received_at,
            received_at=completed_at, effective_at=datetime.combine(changed_on, time.min, _SHANGHAI),
            data_version=version, fields=fields, missing_reasons={}, payload_hash=_text(row, "response_sha256"),
            status="success", error_code=None,
        ))
    for gap in gaps:
        if not isinstance(gap, dict) or set(gap) != {"code", "reason"}:
            raise ValueError("current_industry_gap_invalid")
        code, reason = _text(gap, "code"), _text(gap, "reason")
        if code not in universe or code in codes or reason not in {"current_standard_absent", "current_latest_record_absent", "industry_records_empty"}:
            raise ValueError("current_industry_gap_invalid")
        codes.add(code)
    if codes != set(universe) or not result:
        raise ValueError("industry_reference_snapshot_incomplete")
    return tuple(result)


__all__ = [
    "CurrentIndustryCache",
    "CurrentIndustryDatabaseIncompatibleError",
    "CurrentIndustryPublication",
]

"""Bounded AKShare-compatible research evidence adapter."""

from __future__ import annotations

import hashlib
import logging
import math
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, TypedDict, cast
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from typing_extensions import Unpack

import requests

from trader.domain.market.models import Evidence
from trader.domain.market.research import (
    CorporateRiskFact,
    FinancialReport,
    LongResearchPolicy,
    ResearchAnnouncement,
    ResearchObservation,
    announcement_level,
    corporate_risk_facts_from_announcements,
    reduction_level,
)
from trader.infra.market_data.providers.akshare_news import fetch_news as _fetch_news
from trader.infra.market_data.providers.akshare_parsing import (
    _announcement_rows,
    _clean_text,
    _finite_number,
    _parse_date,
    _parse_date_end,
    _parse_precise_datetime,
    _payload_version,
    _point_in_time,
    _result_rows,
    _source_error,
    _summary_number,
    _validate_code,
)
from trader.infra.persistence.runtime_json import RuntimeJsonWriter, atomic_read_json, atomic_write_json

_LOGGER = logging.getLogger(__name__)


def _corporate_risk_projection(
    announcements: list[ResearchAnnouncement],
    observed_at: datetime,
    version: str,
) -> tuple[tuple[CorporateRiskFact, ...], tuple[Evidence, ...]]:
    facts = corporate_risk_facts_from_announcements(tuple(announcements))
    by_id = {announcement.announcement_id: announcement for announcement in announcements}
    evidence = tuple(
        Evidence(
            evidence_id=fact.evidence_id,
            evidence_type="regulatory_filing",
            title=by_id[fact.evidence_id].title,
            source="issuer_disclosure",
            published_at=fact.announced_at,
            received_at=observed_at,
            data_version=version,
        )
        for fact in facts
        if fact.evidence_id in by_id
    )
    return facts, evidence


def _announcement_history_complete(payload: Mapping[str, object], valid_rows: int) -> bool:
    total_hits = _announcement_total_hits(payload)
    return total_hits is not None and total_hits <= valid_rows


def _financial_history_complete(payload: Mapping[str, object], valid_rows: int) -> bool:
    result = payload.get("result")
    if not isinstance(result, Mapping):
        return False
    total = result.get("count")
    return isinstance(total, int) and not isinstance(total, bool) and total >= 0 and total <= valid_rows


def _announcement_total_hits(payload: Mapping[str, object]) -> int | None:
    data = payload.get("data")
    total = data.get("total_hits") if isinstance(data, Mapping) else None
    return total if isinstance(total, int) and not isinstance(total, bool) and total >= 0 else None


def _announcement_row_identity(row: Mapping[str, object]) -> tuple[str, str, str]:
    return (
        str(row.get("art_code") or ""),
        str(row.get("display_time") or row.get("notice_date") or ""),
        str(row.get("title") or row.get("title_ch") or ""),
    )


def _deduplicate_announcement_rows(
    rows: tuple[Mapping[str, object], ...] | list[Mapping[str, object]],
) -> tuple[Mapping[str, object], ...]:
    result: list[Mapping[str, object]] = []
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        identity = _announcement_row_identity(row)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(dict(row))
    return tuple(result)


def _announcement_payload_with_rows(
    payload: Mapping[str, object],
    rows: tuple[Mapping[str, object], ...],
) -> Mapping[str, object]:
    result = dict(payload)
    raw_data = payload.get("data")
    data = dict(raw_data) if isinstance(raw_data, Mapping) else {}
    data["list"] = [dict(row) for row in rows]
    result["data"] = data
    return result


class HttpResponse(Protocol):
    text: str

    def raise_for_status(self) -> None: ...

    def json(self) -> object: ...


GetFunction = Callable[..., HttpResponse]
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
_DIRECT_PROXIES = {"http": "", "https": "", "all": ""}
_SOURCE_EXCEPTIONS = (OSError, RuntimeError, ValueError, requests.RequestException)
_COMPONENT_TTLS = {
    "financial": timedelta(hours=6),
    "announcement": timedelta(minutes=10),
    "pledge": timedelta(hours=6),
    "unlock": timedelta(hours=6),
}
_ANNOUNCEMENT_PAGE_SIZE = 100
_ANNOUNCEMENT_MAX_PAGES = 50


class _AkshareOptions(TypedDict, total=False):
    timeout_seconds: float
    get: GetFunction | None
    long_research_policy: LongResearchPolicy | None
    evidence_cache_dir: Path | None
    json_writer: RuntimeJsonWriter | None
    cancel_requested: Callable[[], bool]


class AkshareResearchClient:
    def __init__(
        self,
        **options: Unpack[_AkshareOptions],
    ) -> None:
        timeout_seconds = options.get("timeout_seconds", 8.0)
        get = options.get("get")
        self._timeout_seconds = max(0.1, timeout_seconds)
        self._get = get if get is not None else cast(GetFunction, requests.get)
        self._long_research_policy = options.get("long_research_policy")
        self._evidence_cache_dir = options.get("evidence_cache_dir")
        self._json_writer = options.get("json_writer")
        self._cancel_requested = options.get("cancel_requested", lambda: False)

    def fetch_news(self, code: str, *, observed_at: datetime, limit: int = 5) -> tuple[Evidence, ...]:
        return _fetch_news(self, code, observed_at=observed_at, limit=limit)

    def fetch_snapshot(self, code: str, *, observed_at: datetime) -> ResearchObservation:
        _validate_code(code)
        point_in_time = _point_in_time(observed_at)
        policy = self._long_research_policy
        if policy is None:
            raise RuntimeError("long research policy is required for structured research")

        source_errors: list[str] = []
        financial: FinancialReport | None = None
        financial_history: tuple[FinancialReport, ...] = ()
        financial_history_complete = False
        financial_evidence: tuple[Evidence, ...] = ()
        announcements: tuple[ResearchAnnouncement, ...] = ()
        announcement_evidence: tuple[Evidence, ...] = ()
        corporate_risk_facts: tuple[CorporateRiskFact, ...] = ()
        corporate_risk_history_complete = False
        corporate_risk_registry_version = ""
        announcements_available = False
        pledge_ratio: float | None = None
        pledge_evidence: tuple[Evidence, ...] = ()
        unlock_ratio: float | None = None
        unlock_evidence: tuple[Evidence, ...] = ()

        try:
            financial, financial_history, financial_history_complete, financial_evidence = self._fetch_financial(
                code, point_in_time, policy
            )
        except _SOURCE_EXCEPTIONS as exc:
            source_errors.append(_source_error("financial", exc))

        try:
            (
                announcements,
                announcement_evidence,
                corporate_risk_facts,
                corporate_risk_history_complete,
                corporate_risk_registry_version,
            ) = self._fetch_announcements(code, point_in_time, policy)
        except _SOURCE_EXCEPTIONS as exc:
            source_errors.append(_source_error("announcements", exc))
        else:
            announcements_available = True

        try:
            pledge_ratio, pledge_evidence = self._fetch_pledge(code, point_in_time)
        except _SOURCE_EXCEPTIONS as exc:
            source_errors.append(_source_error("pledge", exc))

        try:
            unlock_ratio, unlock_evidence = self._fetch_unlock(code, point_in_time, policy)
        except _SOURCE_EXCEPTIONS as exc:
            source_errors.append(_source_error("unlock", exc))

        announcement_summary = tuple(item for item in announcement_evidence if item.evidence_type == "research_summary")
        announcement_details = tuple(item for item in announcement_evidence if item.evidence_type != "research_summary")
        evidence = (
            *unlock_evidence,
            *pledge_evidence,
            *announcement_summary,
            *financial_evidence,
            *announcement_details,
        )

        return ResearchObservation(
            financial=financial,
            financial_history=financial_history,
            financial_history_complete=financial_history_complete,
            announcements=announcements,
            announcements_available=announcements_available,
            pledge_ratio_pct=pledge_ratio,
            unlock_ratio_pct=unlock_ratio,
            corporate_risk_facts=corporate_risk_facts,
            corporate_risk_history_complete=corporate_risk_history_complete,
            corporate_risk_registry_version=corporate_risk_registry_version,
            evidence=evidence,
            source_errors=tuple(source_errors),
        )

    def fetch_financial_snapshot(self, code: str) -> Mapping[str, object]:
        _validate_code(code)
        payload = self._financial_payload(code)
        rows = _result_rows(payload)
        return dict(rows[0]) if rows else {}

    def _fetch_financial(
        self,
        code: str,
        observed_at: datetime,
        policy: LongResearchPolicy,
    ) -> tuple[FinancialReport | None, tuple[FinancialReport, ...], bool, tuple[Evidence, ...]]:
        payload = self._component_payload(
            "financial",
            code,
            observed_at,
            lambda: self._financial_payload(code),
        )
        history: list[FinancialReport] = []
        for row in _result_rows(payload):
            report_date = _parse_date(row.get("REPORT_DATE"))
            published_at = _parse_date_end(row.get("NOTICE_DATE"))
            if report_date is None or published_at is None or published_at > observed_at:
                continue
            if report_date.month not in {3, 6, 9, 12}:
                continue
            age_days = (observed_at.date() - report_date).days
            if age_days < 0:
                continue
            history.append(
                FinancialReport(
                    report_date=report_date,
                    published_at=published_at,
                    basic_eps=_finite_number(row.get("EPSJB")),
                    book_value_per_share=_finite_number(row.get("BPS")),
                    revenue_growth_pct=_finite_number(row.get("TOTALOPERATEREVETZ")),
                    net_profit_growth_pct=_finite_number(row.get("PARENTNETPROFITTZ")),
                    core_profit_growth_pct=_finite_number(row.get("KCFJCXSYJLRTZ")),
                    roe_pct=_finite_number(row.get("ROEJQ")),
                    parent_net_profit=_finite_number(row.get("PARENTNETPROFIT")),
                    core_net_profit=_finite_number(row.get("KCFJCXSYJLR")),
                )
            )
        ordered_history = tuple(sorted(history, key=lambda item: (item.report_date, item.published_at)))
        current_candidates = tuple(
            report
            for report in ordered_history
            if (observed_at.date() - report.report_date).days <= policy.financial_max_age_days
        )
        history_complete = _financial_history_complete(payload, len(ordered_history))
        if not current_candidates:
            return None, ordered_history, history_complete, ()
        report = max(current_candidates, key=lambda item: (item.report_date, item.published_at))
        version = _payload_version("eastmoney-financial", payload)
        title = (
            f"财务点时：report={report.report_date.isoformat()};EPS={_summary_number(report.basic_eps)};"
            f"BPS={_summary_number(report.book_value_per_share)};rev_yoy={_summary_number(report.revenue_growth_pct)};"
            f"profit_yoy={_summary_number(report.net_profit_growth_pct)};"
            f"core_yoy={_summary_number(report.core_profit_growth_pct)};ROE={_summary_number(report.roe_pct)};"
            f"parent_profit={_summary_number(report.parent_net_profit)};core_profit={_summary_number(report.core_net_profit)}"
        )
        return (
            report,
            ordered_history,
            history_complete,
            (
                Evidence(
                    evidence_id=f"financial:{code}:{version}:{report.report_date.isoformat()}",
                    evidence_type="financial_snapshot",
                    title=title[:240],
                    source="eastmoney_financial",
                    published_at=report.published_at,
                    received_at=observed_at,
                    data_version=version,
                ),
            ),
        )

    def _financial_payload(self, code: str) -> Mapping[str, object]:
        market = "SH" if code.startswith("6") else "SZ"
        return self._request_json(
            "https://datacenter.eastmoney.com/securities/api/data/get",
            params={
                "type": "RPT_F10_FINANCE_MAINFINADATA",
                "sty": "APP_F10_MAINFINADATA",
                "quoteColumns": "",
                "filter": f'(SECUCODE="{code}.{market}")',
                "p": "1",
                "ps": "500",
                "sr": "-1",
                "st": "REPORT_DATE",
                "source": "HSF10",
                "client": "PC",
            },
        )

    def _fetch_announcements(
        self,
        code: str,
        observed_at: datetime,
        policy: LongResearchPolicy,
    ) -> tuple[
        tuple[ResearchAnnouncement, ...],
        tuple[Evidence, ...],
        tuple[CorporateRiskFact, ...],
        bool,
        str,
    ]:
        payload = self._announcement_payload(code, observed_at)
        rows = _announcement_rows(payload)
        cutoff = observed_at - timedelta(days=policy.announcement_lookback_days)
        version = _payload_version("eastmoney-announcement", payload)
        parsed: list[tuple[ResearchAnnouncement, tuple[Evidence, ...], int]] = []
        historical_announcements: list[ResearchAnnouncement] = []
        seen: set[tuple[str, datetime]] = set()
        invalid_rows = 0
        for row in rows:
            title = _clean_text(str(row.get("title") or row.get("title_ch") or ""))
            published_at = _parse_precise_datetime(row.get("display_time"))
            if not title or published_at is None:
                invalid_rows += 1
                continue
            if published_at > observed_at:
                continue
            identity = (str(row.get("art_code") or title), published_at)
            if identity in seen:
                continue
            seen.add(identity)
            art_code = str(row.get("art_code") or "")
            announcement = ResearchAnnouncement(
                title=title[:240],
                published_at=published_at,
                announcement_id=f"announcement:{code}:{art_code or identity[0]}",
                source="issuer_disclosure",
            )
            historical_announcements.append(announcement)
            if published_at < cutoff:
                continue
            negative_level = announcement_level(title, policy)
            ownership_level = reduction_level(title, policy)
            evidence_id = hashlib.sha256(
                f"{code}|{identity[0]}|{published_at.isoformat()}|{title}".encode()
            ).hexdigest()[:32]
            evidence_types = ["announcement"]
            if ownership_level > 0:
                evidence_types.append("ownership_filing")
            if negative_level >= 3:
                evidence_types.append("regulatory_filing")
            parsed.append(
                (
                    announcement,
                    tuple(
                        Evidence(
                            evidence_id=f"announcement:{code}:{evidence_id}:{evidence_type}",
                            evidence_type=evidence_type,
                            title=title[:240],
                            source="eastmoney_announcement",
                            published_at=published_at,
                            received_at=observed_at,
                            data_version=version,
                        )
                        for evidence_type in evidence_types
                    ),
                    max(negative_level, ownership_level),
                )
            )
        if invalid_rows:
            raise ValueError("announcement source returned malformed point-in-time rows")
        parsed.sort(key=lambda item: item[0].published_at, reverse=True)
        parsed = parsed[: policy.announcement_limit]
        evidence_rows = sorted(parsed, key=lambda item: (-item[2], -item[0].published_at.timestamp()))
        titles = tuple(item[0].title for item in parsed)
        positive_hits = sum(any(keyword in title for title in titles) for keyword in policy.policy_positive_keywords)
        negative_hits = sum(any(keyword in title for title in titles) for keyword in policy.policy_negative_keywords)
        maximum_negative_level = max((announcement_level(title, policy) for title in titles), default=0)
        maximum_reduction_level = max((reduction_level(title, policy) for title in titles), default=0)
        summary = Evidence(
            evidence_id=f"announcement:{code}:{version}:summary",
            evidence_type="research_summary",
            title=(
                f"公告派生点时：window={policy.announcement_lookback_days}d;rows={len(parsed)};"
                f"negative_level={maximum_negative_level};reduction_level={maximum_reduction_level};"
                f"policy_positive_hits={positive_hits};policy_negative_hits={negative_hits}"
            ),
            source="eastmoney_announcement",
            published_at=parsed[0][0].published_at if parsed else observed_at,
            received_at=observed_at,
            data_version=version,
        )
        corporate_facts, corporate_evidence = _corporate_risk_projection(
            historical_announcements,
            observed_at,
            version,
        )
        evidence = (summary, *corporate_evidence, *(evidence for item in evidence_rows for evidence in item[1]))
        history_complete = _announcement_history_complete(payload, len(historical_announcements))
        return tuple(item[0] for item in parsed), evidence, corporate_facts, history_complete, version

    def _fetch_pledge(
        self,
        code: str,
        observed_at: datetime,
    ) -> tuple[float, tuple[Evidence, ...]]:
        payload = self._component_payload(
            "pledge",
            code,
            observed_at,
            lambda: self._request_json(
                "https://datacenter-web.eastmoney.com/api/data/v1/get",
                params={
                    "sortColumns": "NOTICE_DATE",
                    "sortTypes": "-1",
                    "pageSize": "200",
                    "pageNumber": "1",
                    "reportName": "RPTA_APP_ACCUMDETAILS",
                    "columns": "ALL",
                    "quoteColumns": "",
                    "source": "WEB",
                    "client": "WEB",
                    "filter": f'(SECURITY_CODE="{code}")',
                },
            ),
        )
        rows = _result_rows(payload)
        eligible: list[tuple[datetime, float]] = []
        invalid_eligible_row = False
        for row in rows:
            published_at = _parse_date_end(row.get("NOTICE_DATE"))
            if published_at is None:
                invalid_eligible_row = True
                continue
            if published_at > observed_at:
                continue
            ratio = _finite_number(row.get("ACCUM_PLEDGE_TSR"))
            if ratio is None or not 0.0 <= ratio <= 100.0:
                invalid_eligible_row = True
            else:
                eligible.append((published_at, ratio))
        if invalid_eligible_row or (rows and not eligible):
            raise ValueError("pledge source returned no valid point-in-time ratio")
        latest = max(eligible, key=lambda item: item[0]) if eligible else None
        ratio = latest[1] if latest is not None else 0.0
        version = _payload_version("eastmoney-pledge", payload)
        return ratio, (
            Evidence(
                evidence_id=f"pledge:{code}:{version}",
                evidence_type="ownership_filing",
                title=f"股权质押点时快照：累计质押占总股本={ratio:.4f}%",
                source="eastmoney_pledge",
                published_at=latest[0] if latest is not None else observed_at,
                received_at=observed_at,
                data_version=version,
            ),
        )

    def _fetch_unlock(
        self,
        code: str,
        observed_at: datetime,
        policy: LongResearchPolicy,
    ) -> tuple[float, tuple[Evidence, ...]]:
        payload = self._component_payload(
            "unlock",
            code,
            observed_at,
            lambda: self._request_json(
                "https://datacenter-web.eastmoney.com/api/data/v1/get",
                params={
                    "sortColumns": "FREE_DATE",
                    "sortTypes": "-1",
                    "pageSize": "200",
                    "pageNumber": "1",
                    "reportName": "RPT_LIFT_STAGE",
                    "columns": (
                        "SECURITY_CODE,SECURITY_NAME_ABBR,FREE_DATE,CURRENT_FREE_SHARES,ABLE_FREE_SHARES,"
                        "LIFT_MARKET_CAP,FREE_RATIO,NEW,B20_ADJCHRATE,A20_ADJCHRATE,FREE_SHARES_TYPE,"
                        "TOTAL_RATIO,NON_FREE_SHARES,BATCH_HOLDER_NUM"
                    ),
                    "source": "WEB",
                    "client": "WEB",
                    "filter": f'(SECURITY_CODE="{code}")',
                },
            ),
        )
        end_date = observed_at.date() + timedelta(days=policy.unlock_forward_days)
        total_ratio = 0.0
        invalid_window_row = False
        for row in _result_rows(payload):
            free_date = _parse_date(row.get("FREE_DATE"))
            if free_date is None:
                invalid_window_row = True
                continue
            if not observed_at.date() <= free_date <= end_date:
                continue
            ratio = _finite_number(row.get("TOTAL_RATIO"))
            if ratio is None or not 0.0 <= ratio <= 1.0:
                invalid_window_row = True
            else:
                total_ratio += ratio * 100.0
        if invalid_window_row or total_ratio > 100.0 + 1e-9:
            raise ValueError("unlock source returned an invalid upcoming ratio")
        version = _payload_version("eastmoney-unlock", payload)
        return total_ratio, (
            Evidence(
                evidence_id=f"unlock:{code}:{version}:{observed_at.date().isoformat()}",
                evidence_type="ownership_filing",
                title=(f"限售解禁点时快照：未来{policy.unlock_forward_days}天累计占总股本={total_ratio:.4f}%"),
                source="eastmoney_unlock",
                published_at=observed_at,
                received_at=observed_at,
                data_version=version,
            ),
        )

    def _request_json(self, url: str, *, params: Mapping[str, object]) -> Mapping[str, object]:
        response = self._request(url, params=params)
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("research source response is not a JSON object")
        return payload

    def _request(
        self,
        url: str,
        *,
        params: Mapping[str, object],
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        self._ensure_running()
        response = self._get(
            url,
            params=params,
            headers=dict(headers or {"Referer": "https://data.eastmoney.com/"}),
            timeout=self._timeout_seconds,
            proxies=_DIRECT_PROXIES,
        )
        response.raise_for_status()
        self._ensure_running()
        return response

    def _ensure_running(self) -> None:
        if self._cancel_requested():
            raise RuntimeError("akshare source lane stopped")

    def _component_payload(
        self,
        source: str,
        code: str,
        observed_at: datetime,
        fetch: Callable[[], Mapping[str, object]],
    ) -> Mapping[str, object]:
        cached = self._read_cached_payload(source, code, observed_at)
        if cached is not None:
            return cached
        payload = fetch()
        self._cache_payload(source, code, observed_at, payload)
        return payload

    def _announcement_payload(self, code: str, observed_at: datetime) -> Mapping[str, object]:
        cached = self._read_cached_payload("announcement", code, observed_at)
        if cached is not None:
            return cached
        stale_record = self._read_cached_payload_record("announcement", code)
        first = self._announcement_page(code, observed_at, 1)
        total = _announcement_total_hits(first)
        first_rows = _deduplicate_announcement_rows(_announcement_rows(first))
        if stale_record is not None and total is not None:
            _cached_at, stale = stale_record
            stale_rows = _deduplicate_announcement_rows(_announcement_rows(stale))
            stale_total = _announcement_total_hits(stale)
            if stale_total is not None and stale_total <= len(stale_rows) and total >= stale_total:
                merged = _deduplicate_announcement_rows((*first_rows, *stale_rows))
                if len(merged) == total:
                    payload = _announcement_payload_with_rows(first, merged)
                    self._cache_payload("announcement", code, observed_at, payload)
                    return payload

        rows = list(first_rows)
        page_count = 1 if total is None else max(1, math.ceil(total / _ANNOUNCEMENT_PAGE_SIZE))
        for page in range(2, min(page_count, _ANNOUNCEMENT_MAX_PAGES) + 1):
            payload = self._announcement_page(code, observed_at, page)
            rows.extend(_announcement_rows(payload))
        combined = _deduplicate_announcement_rows(rows)
        payload = _announcement_payload_with_rows(first, combined)
        self._cache_payload("announcement", code, observed_at, payload)
        return payload

    def _announcement_page(self, code: str, observed_at: datetime, page: int) -> Mapping[str, object]:
        return self._request_json(
            "https://np-anotice-stock.eastmoney.com/api/security/ann",
            params={
                "sr": "-1",
                "page_size": str(_ANNOUNCEMENT_PAGE_SIZE),
                "page_index": str(page),
                "ann_type": "A",
                "client_source": "web",
                "f_node": "0",
                "s_node": "0",
                "stock_list": code,
                "begin_time": "1990-01-01",
                "end_time": observed_at.date().isoformat(),
            },
        )

    def _read_cached_payload(
        self,
        source: str,
        code: str,
        observed_at: datetime,
    ) -> Mapping[str, object] | None:
        record = self._read_cached_payload_record(source, code)
        if record is None:
            return None
        cached_at, payload = record
        age = observed_at - cached_at
        if age < timedelta(0) or age > _COMPONENT_TTLS[source]:
            return None
        return payload

    def _read_cached_payload_record(
        self,
        source: str,
        code: str,
    ) -> tuple[datetime, Mapping[str, object]] | None:
        if self._evidence_cache_dir is None:
            return None
        target = self._evidence_cache_dir / "raw" / source / f"{code}.json"
        try:
            raw = atomic_read_json(target)
            if not isinstance(raw, Mapping):
                return None
            cached_at_raw = raw.get("observed_at")
            payload = raw.get("payload")
            if not isinstance(cached_at_raw, str) or not isinstance(payload, Mapping):
                return None
            cached_at = datetime.fromisoformat(cached_at_raw)
            if cached_at.tzinfo is None:
                return None
            return cached_at, dict(payload)
        except (KeyError, OSError, TypeError, ValueError):
            return None

    def _cache_payload(self, source: str, code: str, observed_at: datetime, payload: object) -> None:
        if self._evidence_cache_dir is None:
            return
        target = self._evidence_cache_dir / "raw" / source / f"{code}.json"
        try:
            writer = self._json_writer.write if self._json_writer is not None else atomic_write_json
            writer(
                target,
                {
                    "source": source,
                    "code": code,
                    "observed_at": observed_at.isoformat(),
                    "payload": payload,
                },
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            _LOGGER.warning("research evidence cache write failed", extra={"source": source, "code": code})


__all__ = ["AkshareResearchClient"]

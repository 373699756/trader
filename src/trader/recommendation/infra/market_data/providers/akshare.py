"""Bounded AKShare-compatible research evidence adapter."""

from __future__ import annotations

import logging
import math
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from typing_extensions import Unpack

import requests

from trader.infra.atomic_files.json import AtomicReadJson, AtomicWriteJson, RuntimeJsonWriter
from trader.infra.cache_contracts import CacheRequestKey, RequestKey
from trader.infra.market_data.providers.akshare_http_contracts import (
    AkshareGetFunction,
    AkshareHttpResponse,
)
from trader.infra.market_data.providers.akshare_parsing import (
    _announcement_rows,
    _clean_text,
    _finite_number,
    _parse_date,
    _parse_date_end,
    _parse_precise_datetime,
    _point_in_time,
    _result_rows,
    _source_error,
    _summary_number,
    _validate_code,
)
from trader.recommendation.domain.market.models import Evidence
from trader.recommendation.domain.market.research import (
    AnnouncementLevel,
    CorporateRiskFact,
    CorporateRiskFactsFromAnnouncements,
    FinancialReport,
    LongResearchPolicy,
    ReductionLevel,
    ResearchAnnouncement,
    ResearchObservation,
)
from trader.recommendation.infra.market_data.providers.akshare_news import FetchNews as _fetch_news

_LOGGER = logging.getLogger(__name__)


def _corporate_risk_projection(
    announcements: list[ResearchAnnouncement],
    observed_at: datetime,
    version: str,
) -> tuple[tuple[CorporateRiskFact, ...], tuple[Evidence, ...]]:
    facts = CorporateRiskFactsFromAnnouncements(tuple(announcements))
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


def _result_metadata(payload: Mapping[str, object]) -> Mapping[str, object] | None:
    result = payload.get("result")
    return result if isinstance(result, Mapping) else None


def _result_page_count(payload: Mapping[str, object]) -> int | None:
    result = _result_metadata(payload)
    pages = result.get("pages") if result is not None else None
    if not isinstance(pages, int) or isinstance(pages, bool) or pages < 0:
        return None
    return max(1, pages)


def _result_count(payload: Mapping[str, object]) -> int | None:
    result = _result_metadata(payload)
    count = result.get("count") if result is not None else None
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        return None
    return count


def _result_payload_with_rows(
    payload: Mapping[str, object],
    rows: Sequence[Mapping[str, object]],
) -> Mapping[str, object]:
    raw_result = _result_metadata(payload)
    if raw_result is None:
        raise ValueError("research batch response is missing result metadata")
    result = dict(raw_result)
    result["data"] = [dict(row) for row in rows]
    result["count"] = len(rows)
    result["pages"] = 1 if rows else 0
    projected = dict(payload)
    projected["result"] = result
    return projected


def _group_result_payloads(
    codes: tuple[str, ...],
    template: Mapping[str, object],
    rows: Sequence[Mapping[str, object]],
) -> Mapping[str, Mapping[str, object]]:
    grouped: dict[str, list[Mapping[str, object]]] = {code: [] for code in codes}
    for row in rows:
        raw_code = row.get("SECURITY_CODE") or row.get("SECUCODE")
        code = str(raw_code or "").split(".", maxsplit=1)[0]
        if code in grouped:
            grouped[code].append(row)
    return {code: _result_payload_with_rows(template, grouped[code]) for code in codes}


def _eastmoney_security_code(code: str) -> str:
    if code.startswith("6"):
        market = "SH"
    elif code.startswith(("4", "8")):
        market = "BJ"
    else:
        market = "SZ"
    return f"{code}.{market}"


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
_RISK_BATCH_SIZE = 50
_RISK_BATCH_PAGE_SIZE = 500
_RISK_BATCH_MAX_PAGES = 50


class _AkshareRequiredOptions(TypedDict):
    get: AkshareGetFunction


class _AkshareOptionalOptions(TypedDict, total=False):
    timeout_seconds: float
    long_research_policy: LongResearchPolicy | None
    evidence_cache_dir: Path | None
    json_writer: RuntimeJsonWriter | None
    cancel_requested: Callable[[], bool]


class _AkshareOptions(_AkshareRequiredOptions, _AkshareOptionalOptions):
    pass


class AkshareResearchClient:
    def __init__(
        self,
        **options: Unpack[_AkshareOptions],
    ) -> None:
        timeout_seconds = options.get("timeout_seconds", 8.0)
        self._timeout_seconds = max(0.1, timeout_seconds)
        self._get = options["get"]
        self._version_lock = threading.Lock()
        self._pledge_date_lock = threading.Lock()
        self._pledge_trade_dates: dict[date, date] = {}
        self._revision = 0
        self._versions: OrderedDict[tuple[str, str], tuple[CacheRequestKey, str]] = OrderedDict()
        self._long_research_policy = options.get("long_research_policy")
        self._evidence_cache_dir = options.get("evidence_cache_dir")
        self._json_writer = options.get("json_writer")
        self._cancel_requested = options.get("cancel_requested", lambda: False)

    def source_version(self, source: str, code: str, payload: Mapping[str, object]) -> str:
        identity = (source, code)
        facts = RequestKey(payload)
        with self._version_lock:
            previous = self._versions.get(identity)
            if previous is not None and previous[0] == facts:
                self._versions.move_to_end(identity)
                return previous[1]
            self._revision += 1
            version = f"{source}:{self._revision}"
            self._versions[identity] = (facts, version)
            self._versions.move_to_end(identity)
            while len(self._versions) > 128:
                self._versions.popitem(last=False)
            return version

    def fetch_news(self, code: str, *, observed_at: datetime, limit: int = 5) -> tuple[Evidence, ...]:
        return _fetch_news(self, code, observed_at=observed_at, limit=limit)

    def fetch_snapshot(self, code: str, *, observed_at: datetime) -> ResearchObservation:
        _validate_code(code)
        point_in_time = _point_in_time(observed_at)
        policy = self._long_research_policy
        if policy is None:
            raise RuntimeError("long research policy is required for structured research")

        return self._compose_snapshot(code, point_in_time, policy, component_payloads=None)

    def fetch_risk_snapshots(
        self,
        codes: Sequence[str],
        *,
        observed_at: datetime,
    ) -> Mapping[str, ResearchObservation]:
        """Fetch current structured risk fields in bounded supplier batches.

        News remains on the candidate-only route.  Announcements are intentionally
        limited to the configured current-event window, so this path never claims
        that the permanent regulatory history is complete.
        """
        normalized = tuple(dict.fromkeys(codes))
        if not normalized:
            return {}
        if len(normalized) > _RISK_BATCH_SIZE:
            raise ValueError(f"risk snapshot batch accepts at most {_RISK_BATCH_SIZE} codes")
        for code in normalized:
            _validate_code(code)
        point_in_time = _point_in_time(observed_at)
        policy = self._long_research_policy
        if policy is None:
            raise RuntimeError("long research policy is required for structured research")

        payloads: dict[str, dict[str, Mapping[str, object]]] = {code: {} for code in normalized}
        errors: dict[str, list[str]] = {code: ["announcements:deferred"] for code in normalized}
        fetchers = (
            ("financial", lambda: self._batch_financial_payloads(normalized)),
            ("pledge", lambda: self._batch_pledge_payloads(normalized, point_in_time)),
            ("unlock", lambda: self._batch_unlock_payloads(normalized, point_in_time, policy)),
        )
        for source, fetch in fetchers:
            try:
                component_payloads = fetch()
            except _SOURCE_EXCEPTIONS as exc:
                error = _source_error(source, exc)
                for code in normalized:
                    errors[code].append(error)
                continue
            for code in normalized:
                payloads[code][source] = component_payloads[code]

        return {
            code: self._compose_snapshot(
                code,
                point_in_time,
                policy,
                component_payloads=payloads[code],
                initial_errors=errors[code],
            )
            for code in normalized
        }

    def _compose_snapshot(
        self,
        code: str,
        point_in_time: datetime,
        policy: LongResearchPolicy,
        *,
        component_payloads: Mapping[str, Mapping[str, object]] | None,
        initial_errors: Sequence[str] = (),
    ) -> ResearchObservation:
        failed_sources = {error.partition(":")[0] for error in initial_errors}

        source_errors = list(initial_errors)
        financial: FinancialReport | None = None
        financial_history: tuple[FinancialReport, ...] = ()
        financial_history_complete = False
        financial_evidence: tuple[Evidence, ...] = ()
        announcements: tuple[ResearchAnnouncement, ...] = ()
        announcement_evidence: tuple[Evidence, ...] = ()
        corporate_risk_facts: tuple[CorporateRiskFact, ...] = ()
        corporate_risk_history_complete = False
        corporate_risk_evidence_version = ""
        announcements_available = False
        pledge_ratio: float | None = None
        pledge_evidence: tuple[Evidence, ...] = ()
        unlock_ratio: float | None = None
        unlock_evidence: tuple[Evidence, ...] = ()
        news_evidence: tuple[Evidence, ...] = ()

        if component_payloads is None:
            try:
                news_evidence = self.fetch_news(code, observed_at=point_in_time)
            except _SOURCE_EXCEPTIONS as exc:
                source_errors.append(_source_error("news", exc))

        if "financial" not in failed_sources:
            try:
                financial, financial_history, financial_history_complete, financial_evidence = self._fetch_financial(
                    code,
                    point_in_time,
                    policy,
                    payload=None if component_payloads is None else component_payloads["financial"],
                )
            except _SOURCE_EXCEPTIONS as exc:
                source_errors.append(_source_error("financial", exc))

        if "announcements" not in failed_sources:
            try:
                (
                    announcements,
                    announcement_evidence,
                    corporate_risk_facts,
                    corporate_risk_history_complete,
                    corporate_risk_evidence_version,
                ) = self._fetch_announcements(
                    code,
                    point_in_time,
                    policy,
                    payload=None if component_payloads is None else component_payloads["announcements"],
                    history_complete_allowed=component_payloads is None,
                )
            except _SOURCE_EXCEPTIONS as exc:
                source_errors.append(_source_error("announcements", exc))
            else:
                announcements_available = True

        if "pledge" not in failed_sources:
            try:
                pledge_ratio, pledge_evidence = self._fetch_pledge(
                    code,
                    point_in_time,
                    payload=None if component_payloads is None else component_payloads["pledge"],
                )
            except _SOURCE_EXCEPTIONS as exc:
                source_errors.append(_source_error("pledge", exc))

        if "unlock" not in failed_sources:
            try:
                unlock_ratio, unlock_evidence = self._fetch_unlock(
                    code,
                    point_in_time,
                    policy,
                    payload=None if component_payloads is None else component_payloads["unlock"],
                )
            except _SOURCE_EXCEPTIONS as exc:
                source_errors.append(_source_error("unlock", exc))

        announcement_summary = tuple(item for item in announcement_evidence if item.evidence_type == "research_summary")
        announcement_details = tuple(item for item in announcement_evidence if item.evidence_type != "research_summary")
        evidence = (
            *news_evidence,
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
            corporate_risk_evidence_version=corporate_risk_evidence_version,
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
        *,
        payload: Mapping[str, object] | None = None,
    ) -> tuple[FinancialReport | None, tuple[FinancialReport, ...], bool, tuple[Evidence, ...]]:
        if payload is None:
            payload = self._component_payload(
                "financial",
                code,
                observed_at,
                lambda: self._financial_payload(code),
            )
        history: list[FinancialReport] = []
        for row in _result_rows(payload):
            report_date = _parse_date(row.get("REPORT_DATE"))
            published_at = _parse_date_end(row.get("NOTICE_DATE") or row.get("TRADE_DATE"))
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
        version = self.source_version("eastmoney-financial", code, payload)
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
        return self._request_json(
            "https://datacenter.eastmoney.com/securities/api/data/get",
            params={
                "type": "RPT_F10_FINANCE_MAINFINADATA",
                "sty": "APP_F10_MAINFINADATA",
                "quoteColumns": "",
                "filter": f'(SECUCODE="{_eastmoney_security_code(code)}")',
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
        *,
        payload: Mapping[str, object] | None = None,
        history_complete_allowed: bool = True,
    ) -> tuple[
        tuple[ResearchAnnouncement, ...],
        tuple[Evidence, ...],
        tuple[CorporateRiskFact, ...],
        bool,
        str,
    ]:
        if payload is None:
            payload = self._announcement_payload(code, observed_at)
        rows = _announcement_rows(payload)
        cutoff = observed_at - timedelta(days=policy.announcement_lookback_days)
        version = self.source_version("eastmoney-announcement", code, payload)
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
            negative_level = AnnouncementLevel(title, policy)
            ownership_level = ReductionLevel(title, policy)
            evidence_id = art_code or f"{version}:{len(parsed)}"
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
        parsed.sort(key=lambda item: item[0].published_at, reverse=True)
        parsed = parsed[: policy.announcement_limit]
        evidence_rows = sorted(parsed, key=lambda item: (-item[2], -item[0].published_at.timestamp()))
        titles = tuple(item[0].title for item in parsed)
        positive_hits = sum(any(keyword in title for title in titles) for keyword in policy.policy_positive_keywords)
        negative_hits = sum(any(keyword in title for title in titles) for keyword in policy.policy_negative_keywords)
        maximum_negative_level = max((AnnouncementLevel(title, policy) for title in titles), default=0)
        maximum_reduction_level = max((ReductionLevel(title, policy) for title in titles), default=0)
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
        history_complete = (
            history_complete_allowed
            and invalid_rows == 0
            and _announcement_history_complete(payload, len(historical_announcements))
        )
        return tuple(item[0] for item in parsed), evidence, corporate_facts, history_complete, version

    def _fetch_pledge(
        self,
        code: str,
        observed_at: datetime,
        *,
        payload: Mapping[str, object] | None = None,
    ) -> tuple[float, tuple[Evidence, ...]]:
        if payload is None:
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
            if published_at is None and (trade_date := _parse_date(row.get("TRADE_DATE"))) is not None:
                published_at = datetime.combine(trade_date, time.min, tzinfo=SHANGHAI_TZ)
            if published_at is None:
                invalid_eligible_row = True
                continue
            if published_at > observed_at:
                continue
            ratio = _finite_number(row.get("ACCUM_PLEDGE_TSR") or row.get("PLEDGE_RATIO"))
            if ratio is None or not 0.0 <= ratio <= 100.0:
                invalid_eligible_row = True
            else:
                eligible.append((published_at, ratio))
        if invalid_eligible_row or (rows and not eligible):
            raise ValueError("pledge source returned no valid point-in-time ratio")
        latest = max(eligible, key=lambda item: item[0]) if eligible else None
        ratio = latest[1] if latest is not None else 0.0
        version = self.source_version("eastmoney-pledge", code, payload)
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
        *,
        payload: Mapping[str, object] | None = None,
    ) -> tuple[float, tuple[Evidence, ...]]:
        if payload is None:
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
        version = self.source_version("eastmoney-unlock", code, payload)
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

    def _batch_financial_payloads(
        self,
        codes: tuple[str, ...],
    ) -> Mapping[str, Mapping[str, object]]:
        filters = ",".join(f'"{_eastmoney_security_code(code)}"' for code in codes)
        template, rows = self._batch_result_rows(
            "https://datacenter.eastmoney.com/securities/api/data/get",
            params={
                "type": "RPT_F10_FINANCE_MAINFINADATA",
                "sty": "APP_F10_MAINFINADATA",
                "quoteColumns": "",
                "filter": f"(SECUCODE in ({filters}))",
                "sr": "-1",
                "st": "REPORT_DATE",
                "source": "HSF10",
                "client": "PC",
            },
            page_key="p",
            size_key="ps",
        )
        return _group_result_payloads(codes, template, rows)

    def _batch_pledge_payloads(
        self,
        codes: tuple[str, ...],
        observed_at: datetime,
    ) -> Mapping[str, Mapping[str, object]]:
        filters = ",".join(f'"{code}"' for code in codes)
        trade_date = self._latest_pledge_trade_date(observed_at)
        template, rows = self._batch_result_rows(
            "https://datacenter-web.eastmoney.com/api/data/v1/get",
            params={
                "sortColumns": "PLEDGE_RATIO",
                "sortTypes": "-1",
                "reportName": "RPT_CSDC_LIST",
                "columns": "ALL",
                "quoteColumns": "",
                "source": "WEB",
                "client": "WEB",
                "filter": (f"(TRADE_DATE='{trade_date.isoformat()}')(SECURITY_CODE in ({filters}))"),
            },
            page_key="pageNumber",
            size_key="pageSize",
        )
        return _group_result_payloads(codes, template, rows)

    def _latest_pledge_trade_date(self, observed_at: datetime) -> date:
        observation_date = observed_at.date()
        with self._pledge_date_lock:
            cached = self._pledge_trade_dates.get(observation_date)
            if cached is not None:
                return cached
            payload = self._request_json(
                "https://datacenter-web.eastmoney.com/api/data/v1/get",
                params={
                    "sortColumns": "TRADE_DATE",
                    "sortTypes": "-1",
                    "pageSize": "1",
                    "pageNumber": "1",
                    "reportName": "RPT_CSDC_LIST",
                    "columns": "ALL",
                    "quoteColumns": "",
                    "source": "WEB",
                    "client": "WEB",
                    "filter": f"(TRADE_DATE<='{observation_date.isoformat()}')",
                },
            )
            rows = _result_rows(payload)
            trade_date = _parse_date(rows[0].get("TRADE_DATE")) if rows else None
            if trade_date is None or trade_date > observation_date:
                raise ValueError("pledge batch has no valid point-in-time snapshot date")
            self._pledge_trade_dates[observation_date] = trade_date
            return trade_date

    def _batch_unlock_payloads(
        self,
        codes: tuple[str, ...],
        observed_at: datetime,
        policy: LongResearchPolicy,
    ) -> Mapping[str, Mapping[str, object]]:
        filters = ",".join(f'"{code}"' for code in codes)
        start_date = observed_at.date()
        end_date = start_date + timedelta(days=policy.unlock_forward_days)
        template, rows = self._batch_result_rows(
            "https://datacenter-web.eastmoney.com/api/data/v1/get",
            params={
                "sortColumns": "FREE_DATE",
                "sortTypes": "-1",
                "reportName": "RPT_LIFT_STAGE",
                "columns": (
                    "SECURITY_CODE,SECURITY_NAME_ABBR,FREE_DATE,CURRENT_FREE_SHARES,ABLE_FREE_SHARES,"
                    "LIFT_MARKET_CAP,FREE_RATIO,NEW,B20_ADJCHRATE,A20_ADJCHRATE,FREE_SHARES_TYPE,"
                    "TOTAL_RATIO,NON_FREE_SHARES,BATCH_HOLDER_NUM"
                ),
                "source": "WEB",
                "client": "WEB",
                "filter": (
                    f"(FREE_DATE>='{start_date.isoformat()}')"
                    f"(FREE_DATE<='{end_date.isoformat()}')"
                    f"(SECURITY_CODE in ({filters}))"
                ),
            },
            page_key="pageNumber",
            size_key="pageSize",
        )
        return _group_result_payloads(codes, template, rows)

    def _batch_result_rows(
        self,
        url: str,
        *,
        params: Mapping[str, object],
        page_key: str,
        size_key: str,
    ) -> tuple[Mapping[str, object], tuple[Mapping[str, object], ...]]:
        first = self._request_json(
            url,
            params={**params, page_key: "1", size_key: str(_RISK_BATCH_PAGE_SIZE)},
        )
        page_count = _result_page_count(first)
        if page_count is None:
            raise ValueError("research batch response is missing page metadata")
        if page_count > _RISK_BATCH_MAX_PAGES:
            raise ValueError("research batch exceeds bounded page limit")
        rows = list(_result_rows(first))
        for page in range(2, page_count + 1):
            payload = self._request_json(
                url,
                params={**params, page_key: str(page), size_key: str(_RISK_BATCH_PAGE_SIZE)},
            )
            rows.extend(_result_rows(payload))
        expected = _result_count(first)
        if expected is None or len(rows) < expected:
            raise ValueError("research batch returned incomplete pages")
        return first, tuple(rows)

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
    ) -> AkshareHttpResponse:
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
            raw = AtomicReadJson(target)
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
            writer = self._json_writer.write if self._json_writer is not None else AtomicWriteJson
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

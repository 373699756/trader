"""Read official same-period material for the existing bounded financial-null audit."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from trader.download.domain.company_announcement_numbers import ExtractCompanyAnnouncementNumbers
from trader.download.application.company_failures import CompanyFailureCode
from trader.download.domain.company_evidence import DisclosurePartition, DisclosureQuery, EvidenceMarket
from trader.download.infra.company_disclosure_source import CninfoMarketDisclosureSource
from trader.download.infra.company_raw_files import CompanyRawFiles
from trader.download.infra.company_document_process import CaptureDocumentTool
from scripts.core.projection_values import SafeNonnegativeInt


_ZONE = ZoneInfo("Asia/Shanghai")
_LABELS = {
    "eps": r"基本每股收益",
    "roe_pct": r"加权平均净资产",
    "core_profit_growth_pct": r"扣除非|非经常性损益",
    "revenue_growth_pct": r"营业收入",
}


@dataclass(frozen=True, slots=True)
class _Request:
    code: str
    report_date: date
    disclosed_at: datetime
    fields: tuple[str, ...]


def _requests(path: Path) -> tuple[_Request, ...]:
    with path.open("rb") as stream:
        content = stream.read(8 * 1024 * 1024 + 1)
    if len(content) > 8 * 1024 * 1024:
        raise ValueError("company_official_financial_baseline_too_large")
    report = json.loads(content)
    audits = [check["summary"]["database"]["publication"]["field_audit"]["financial_sources"]["latest"]
              for check in report["checks"] if check["name"] == "company_resume"]
    if len(audits) != 1 or audits[0]["items_truncated"]:
        raise ValueError("company_official_financial_baseline_incomplete")
    requests: list[_Request] = []
    for item in audits[0]["items"]:
        code, period = item["code"], date.fromisoformat(item["report_date"])
        EvidenceMarket(code)
        fields = tuple(entry["field"] for entry in item["fields"] if entry["reason"] == "source_null")
        if not fields:
            continue
        if any(field not in _LABELS for field in fields) or (period.month, period.day) != (9, 30):
            raise ValueError("company_official_financial_period_or_field_unsupported")
        if type(item["disclosed_at"]) is not int:
            raise ValueError("company_official_financial_disclosure_invalid")
        disclosed = (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=item["disclosed_at"])).astimezone(_ZONE)
        requests.append(_Request(code, period, disclosed, fields))
    if not 1 <= len(requests) <= 50 or len({request.code for request in requests}) != len(requests):
        raise ValueError("company_official_financial_baseline_selection_invalid")
    return tuple(requests)


def AuditCompanyOfficialFinancialSources(
    source: CninfoMarketDisclosureSource, baseline: Path, *, observed_at: datetime, deadline: float,
    raw_files: CompanyRawFiles | None = None, pdftotext: str | None = None,
) -> dict[str, object]:
    """Project exact source excerpts; never infer a metric or write a business database."""
    results: list[dict[str, object]] = []
    requests = _requests(baseline)
    for request in requests:
        item: dict[str, object] = {
            "code": request.code, "report_date": request.report_date.isoformat(),
            "baseline_disclosed_at": request.disclosed_at.isoformat(), "fields": request.fields,
            "status": "verification_required", "sources": [],
        }
        results.append(item)
        if time.monotonic() >= deadline:
            item["reason"] = "task_deadline_exhausted"
            continue
        try:
            day = request.disclosed_at.date()
            plate = "shkcp" if request.code.startswith("688") else "shmb" if request.code.startswith("6") else (
                "szcy" if request.code.startswith(("30",)) else "szmb"
            )
            partition = DisclosurePartition(EvidenceMarket(request.code), day - timedelta(days=1), day + timedelta(days=1),
                                            query=DisclosureQuery(plate))
            members = tuple(member for query in source.split_queries(partition) for member in query.securities
                            if member.code == request.code)
            if len(members) != 1:
                raise ValueError("company_official_financial_issuer_unavailable")
            partition = replace(partition, query=DisclosureQuery(plate, members))
            page = source.fetch(partition)
            directory = list(page.rows)
            pages = 1
            while page.has_more and pages < 3:
                if time.monotonic() >= deadline:
                    raise TimeoutError("company_official_financial_deadline_exhausted")
                following = source.fetch(replace(partition, page=pages + 1))
                if following.total != page.total or {row.identity for row in directory}.intersection(row.identity for row in following.rows):
                    raise ValueError("company_official_financial_directory_changed")
                directory.extend(following.rows)
                page = following
                pages += 1
            item["directory_pages"] = pages
            if page.has_more:
                raise ValueError("company_official_financial_directory_limit")
            rows = tuple(row for row in directory if row.codes == (request.code,) and row.published_at <= observed_at
                         and re.search(rf"{request.report_date.year}年(?:第三季度|三季度|三季)报告", "".join(row.title.split())))
            sources: list[dict[str, object]] = []
            item["sources"] = sources
            if not rows:
                item["reason"] = "official_period_report_not_found"
            for row in rows[:4]:
                if time.monotonic() >= deadline:
                    raise TimeoutError("company_official_financial_deadline_exhausted")
                source.download_document(row)
                extraction = source.extract_document(row)
                text = extraction.text
                material: dict[str, object] = {
                    "source": row.source, "source_key": row.identity, "title": row.title,
                    "url": row.url, "published_at": row.published_at.isoformat(), "record_hash": row.digest,
                    "body_hash": hashlib.sha256(text.encode()).hexdigest(),
                    "extraction_method": extraction.method, "extraction_policy": extraction.policy_id,
                    "issuer_codes": ExtractCompanyAnnouncementNumbers(text).issuer_codes, "excerpts": _excerpts(text, request.fields),
                    "revision_material": any(word in row.title for word in ("修订", "更正")),
                }
                sources.append(material)
                if raw_files is not None and pdftotext is not None:
                    local = raw_files.local(row)
                    if local is None:
                        raise ValueError("company_official_financial_pdf_missing")
                    parsing_deadline = min(deadline, time.monotonic() + 10)

                    def check() -> None:
                        if time.monotonic() >= parsing_deadline:
                            raise TimeoutError("company_official_financial_layout_timeout")

                    code, output = CaptureDocumentTool(
                        (pdftotext, "-layout", "-f", "1", "-l", "4", "-enc", "UTF-8", str(local.path), "-"),
                        maximum_bytes=4 * 1024 * 1024, check=check,
                    )
                    if code:
                        raise ValueError("company_official_financial_layout_unavailable")
                    layout_text = output.decode("utf-8")
                    header_lines = layout_text.splitlines()
                    header = next((index for index, line in enumerate(header_lines)
                                   if re.search(r"主要会计数据|主要财务指标|主要财务数据", "".join(line.split()))), None)
                    material.update({
                        "pdf_sha256": local.payload_hash.hex(), "layout_sha256": hashlib.sha256(output).hexdigest(),
                        "table_header": "\n".join(header_lines[header:header + 20])[:1000] if header is not None else None,
                        "table_excerpts": _excerpts(layout_text, request.fields),
                    })
            if len(rows) > 4:
                item["reason"] = "official_period_versions_exceed_limit"
            elif rows:
                item["reason"] = "official_excerpts_require_value_and_column_annotation"
        except (OSError, ValueError, RuntimeError) as exc:
            item["reason"] = CompanyFailureCode(exc)
    return {"status": "degraded", "official_financial_probe": {
        "read_only": True, "production_adopted": False,
        "field_correctness_verified": False, "observed_at": observed_at.isoformat(),
        "baseline_sha256": hashlib.sha256(baseline.read_bytes()).hexdigest(),
        "stocks": len(requests), "fields": sum(len(request.fields) for request in requests), "items": results,
    }}


def _excerpts(text: str, fields: tuple[str, ...]) -> list[dict[str, object]]:
    lines = text[:24000].splitlines()
    excerpts: list[dict[str, object]] = []
    for field in fields:
        matches: list[dict[str, object]] = []
        previous = -4
        for index in range(len(lines)):
            if index <= previous + 3 or re.search(_LABELS[field], "".join("".join(lines[index:index + 3]).split())) is None:
                continue
            matches.append({"start_line": max(1, index - 2), "quote": "\n".join(lines[max(0, index - 3):index + 9])[:1000]})
            previous = index
            if len(matches) == 3:
                break
        excerpts.append({"field": field, "matches": matches})
    return excerpts


def ProjectCompanyOfficialFinancialAudit(source: Mapping[str, object]) -> dict[str, object]:
    """Keep field names, source identities and excerpts bounded at the public boundary."""
    def rows(value: object, limit: int) -> list[Mapping[str, object]]:
        return [row for row in value[:limit] if isinstance(row, dict)] if isinstance(value, list) else []

    def text(value: object, limit: int = 160) -> str | None:
        return value[:limit] if isinstance(value, str) else None

    def names(value: object) -> list[str]:
        return [name for name in value[:8] if isinstance(name, str) and name in _LABELS] if isinstance(value, (list, tuple)) else []

    def issuer_codes(value: object) -> list[str]:
        return [code for code in value[:8] if isinstance(code, str) and re.fullmatch(r"[0-9]{6}", code)] if isinstance(value, (list, tuple)) else []

    return {
        "read_only": source.get("read_only") is True, "production_adopted": False, "field_correctness_verified": False,
        "observed_at": text(source.get("observed_at")), "baseline_sha256": text(source.get("baseline_sha256"), 64),
        "stocks": SafeNonnegativeInt(source.get("stocks")), "fields": SafeNonnegativeInt(source.get("fields")),
        "items": [{
            **{name: text(item.get(name)) for name in ("code", "report_date", "baseline_disclosed_at", "status", "reason")},
            "fields": names(item.get("fields")),
            "directory_pages": SafeNonnegativeInt(item.get("directory_pages")),
            "sources": [{
                **{name: text(material.get(name), 512) for name in (
                    "source", "source_key", "title", "url", "published_at", "record_hash", "body_hash", "pdf_sha256", "layout_sha256",
                    "extraction_method", "extraction_policy",
                )},
                "revision_material": material.get("revision_material") is True,
                "issuer_codes": issuer_codes(material.get("issuer_codes")),
                "table_header": text(material.get("table_header"), 1000),
                **{name: [{"field": text(excerpt.get("field")), "matches": [
                    {"start_line": SafeNonnegativeInt(match.get("start_line")), "quote": text(match.get("quote"), 1000)}
                    for match in rows(excerpt.get("matches"), 3)
                ]} for excerpt in rows(material.get(name), 8)] for name in ("excerpts", "table_excerpts")},
            } for material in rows(item.get("sources"), 4)],
        } for item in rows(source.get("items"), 50)],
    }


__all__ = ["AuditCompanyOfficialFinancialSources", "ProjectCompanyOfficialFinancialAudit"]

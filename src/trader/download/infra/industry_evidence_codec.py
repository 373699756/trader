"""Strict codec for current official CAPCO rows; source documents stay separate."""

from __future__ import annotations

from datetime import date, datetime
from typing import cast

from trader.download.domain.industry_evidence import OfficialIndustryEvidence
from trader.download.infra.official_industry_release import ValidateOfficialIndustryUrl

_EVIDENCE_KEYS = frozenset({
    "code", "source_url", "document_sha256", "disclosed_on", "effective_from", "effective_to",
    "received_at", "page", "excerpt", "native_category", "native_name", "announcement_url", "announcement_sha256",
})


def IndustryEvidencePayload(item: OfficialIndustryEvidence) -> dict[str, object]:
    return {
        "code": item.code,
        "source_url": item.source_url,
        "document_sha256": item.document_sha256,
        "disclosed_on": item.disclosed_on.isoformat(),
        "effective_from": item.effective_from.isoformat(),
        "effective_to": item.effective_to.isoformat(),
        "received_at": item.received_at.isoformat(),
        "page": item.page,
        "excerpt": item.excerpt,
        "native_category": item.native_category,
        "native_name": item.native_name,
        "announcement_url": item.announcement_url,
        "announcement_sha256": item.announcement_sha256,
    }


def ParseIndustryEvidence(value: object) -> OfficialIndustryEvidence:
    if not isinstance(value, dict) or set(value) != _EVIDENCE_KEYS:
        raise ValueError("industry_evidence_fields_invalid")
    raw = cast(dict[str, object], value)
    ValidateOfficialIndustryUrl(_text(raw, "source_url"), attachment=True)
    ValidateOfficialIndustryUrl(_text(raw, "announcement_url"))
    return OfficialIndustryEvidence(
        _text(raw, "code"),
        _text(raw, "source_url"),
        _text(raw, "document_sha256"),
        date.fromisoformat(_text(raw, "disclosed_on")),
        date.fromisoformat(_text(raw, "effective_from")),
        date.fromisoformat(_text(raw, "effective_to")),
        datetime.fromisoformat(_text(raw, "received_at")),
        _integer(raw, "page"),
        _text(raw, "excerpt"),
        _text(raw, "native_category"),
        _text(raw, "native_name"),
        _text(raw, "announcement_url"),
        _text(raw, "announcement_sha256"),
    )


def _text(raw: dict[str, object], key: str) -> str:
    value = raw[key]
    if not isinstance(value, str) or not value:
        raise ValueError("industry_evidence_text_invalid")
    return value


def _integer(raw: dict[str, object], key: str) -> int:
    value = raw[key]
    if type(value) is not int:
        raise ValueError("industry_evidence_integer_invalid")
    return value


__all__ = [
    "IndustryEvidencePayload",
    "ParseIndustryEvidence",
]

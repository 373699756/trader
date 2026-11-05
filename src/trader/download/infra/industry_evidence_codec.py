"""Bounded, explicit codec and local-document verification for reviewed evidence."""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from io import BytesIO
import json
from pathlib import Path
import re
from typing import TYPE_CHECKING, cast
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from pypdf import PdfReader

from trader.download.domain.industry_evidence import IndustryCategoryMapping, OfficialIndustryEvidence

_OFFICIAL_HOSTS = frozenset({
    "disc.static.szse.cn", "www.szse.cn", "static.cninfo.com.cn", "www.cninfo.com.cn",
    "www.sse.com.cn", "static.sse.com.cn", "reportdocs.static.sse.org.cn", "www.bse.cn",
    "www.csrc.gov.cn", "www.capco.org.cn", "www.csisc.cn",
})
_EVIDENCE_KEYS = frozenset({
    "code", "source_url", "document_sha256", "disclosed_at", "effective_from", "effective_to",
    "received_at", "page", "excerpt", "mapping",
})
_MAPPING_KEYS = frozenset({
    "native_classification", "native_code", "native_name", "target_industry", "reference_url",
    "reference_sha256", "reference_page", "reference_excerpt", "rationale",
})


def IndustryEvidencePayload(item: OfficialIndustryEvidence) -> dict[str, object]:
    mapping = item.mapping
    return {
        "code": item.code, "source_url": item.source_url, "document_sha256": item.document_sha256,
        "disclosed_at": item.disclosed_at.isoformat(), "effective_from": item.effective_from.isoformat(),
        "effective_to": item.effective_to.isoformat(), "received_at": item.received_at.isoformat(),
        "page": item.page, "excerpt": item.excerpt,
        "mapping": {
            "native_classification": mapping.native_classification, "native_code": mapping.native_code,
            "native_name": mapping.native_name, "target_industry": mapping.target_industry,
            "reference_url": mapping.reference_url, "reference_sha256": mapping.reference_sha256,
            "reference_page": mapping.reference_page, "reference_excerpt": mapping.reference_excerpt,
            "rationale": mapping.rationale,
        },
    }


def ParseIndustryEvidence(value: object) -> OfficialIndustryEvidence:
    raw = _object(value, _EVIDENCE_KEYS)
    mapping = _object(raw["mapping"], _MAPPING_KEYS)
    _official_url(_text(raw, "source_url"))
    _official_url(_text(mapping, "reference_url"))
    return OfficialIndustryEvidence(
        _text(raw, "code"),
        _text(raw, "source_url"),
        _text(raw, "document_sha256"),
        datetime.fromisoformat(_text(raw, "disclosed_at")),
        date.fromisoformat(_text(raw, "effective_from")),
        date.fromisoformat(_text(raw, "effective_to")),
        datetime.fromisoformat(_text(raw, "received_at")),
        _integer(raw, "page"),
        _text(raw, "excerpt"),
        IndustryCategoryMapping(
            _text(mapping, "native_classification"),
            _text(mapping, "native_code"),
            _text(mapping, "native_name"),
            _text(mapping, "target_industry"),
            _text(mapping, "reference_url"),
            _text(mapping, "reference_sha256"),
            _integer(mapping, "reference_page"),
            _text(mapping, "reference_excerpt"),
            _text(mapping, "rationale"),
        ),
    )


def LoadReviewedIndustryEvidence(path: Path) -> tuple[OfficialIndustryEvidence, ...]:
    """Read explicit reviewed metadata and verify both local source documents; never fetch."""
    if not path.is_file():
        raise ValueError("industry_evidence_input_not_file")
    with path.open("rb") as stream:
        payload = stream.read(2 * 1024 * 1024 + 1)
    if len(payload) > 2 * 1024 * 1024:
        raise ValueError("industry_evidence_input_too_large")
    values: object = json.loads(payload)
    if not isinstance(values, list) or not 1 <= len(values) <= 1000:
        raise ValueError("industry_evidence_input_invalid")
    results: list[OfficialIndustryEvidence] = []
    for value in values:
        raw = _object(value, frozenset({"evidence", "document", "mapping_document"}))
        item = ParseIndustryEvidence(raw["evidence"])
        with (
            _pdf(path.parent, _text(raw, "document"), item.document_sha256) as source,
            _pdf(path.parent, _text(raw, "mapping_document"), item.mapping.reference_sha256) as mapping,
        ):
            cover = _normalize(_page_text(source, 1))
            issuer_codes = set(re.findall(r"(?:股票|证券)代码[:：]([0-9]{6})", cover))
            if issuer_codes != {item.code}:
                raise ValueError("industry_evidence_issuer_mismatch")
            excerpt = _normalize(item.excerpt)
            if any(_normalize(text) not in excerpt for text in (
                item.mapping.native_classification, item.mapping.native_code, item.mapping.native_name,
            )):
                raise ValueError("industry_evidence_classification_missing")
            _verify_excerpt(source, item.page, item.excerpt)
            _verify_excerpt(mapping, item.mapping.reference_page, item.mapping.reference_excerpt)
            target = re.sub(r"\s+", "", item.mapping.target_industry)
            if target not in _normalize(item.mapping.reference_excerpt):
                raise ValueError("industry_mapping_target_missing")
        results.append(item)
    if len({item.identity for item in results}) != len(results):
        raise ValueError("industry_evidence_duplicate")
    return tuple(results)


def _pdf(root: Path, raw_path: str, digest: str) -> PdfReader:
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    path = (root / raw_path).resolve()
    if Path(raw_path).is_absolute() or not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError("industry_evidence_document_path_invalid")
    with path.open("rb") as stream:
        payload = stream.read(32 * 1024 * 1024 + 1)
    if len(payload) > 32 * 1024 * 1024 or hashlib.sha256(payload).hexdigest() != digest:
        raise ValueError("industry_evidence_document_hash_mismatch")
    buffer = BytesIO(payload)
    completed = False
    try:
        reader = PdfReader(buffer)
        if reader.is_encrypted or not 1 <= len(reader.pages) <= 1000:
            raise ValueError("industry_evidence_document_invalid")
        completed = True
    except (PyPdfError, RecursionError) as exc:
        raise ValueError("industry_evidence_document_invalid") from exc
    finally:
        if not completed:
            buffer.close()
    return reader


def _verify_excerpt(reader: PdfReader, page: int, excerpt: str) -> None:
    if page > len(reader.pages) or _normalize(excerpt) not in _normalize(_page_text(reader, page)):
        raise ValueError("industry_evidence_excerpt_mismatch")


def _page_text(reader: PdfReader, page: int) -> str:
    from pypdf.errors import PyPdfError

    try:
        return reader.pages[page - 1].extract_text() or ""
    except (PyPdfError, RecursionError, TypeError, ValueError) as exc:
        raise ValueError("industry_evidence_text_invalid") from exc


def _normalize(value: str) -> str:
    return re.sub(r"\s+", "", value).translate(str.maketrans("（）", "()", "《》"))


def _official_url(value: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname not in _OFFICIAL_HOSTS or parsed.username or parsed.password:
        raise ValueError("industry_evidence_url_invalid")


def _object(value: object, keys: frozenset[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("industry_evidence_fields_invalid")
    return cast(dict[str, object], value)


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
    "LoadReviewedIndustryEvidence",
    "ParseIndustryEvidence",
]

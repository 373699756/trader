"""Explicit persistence-only snapshot codec; normalized tables remain the write model."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date, datetime
from typing import Literal, cast

from trader.download.domain.company_evidence import (
    CompanyDocument,
    CompanyEvidenceSnapshot,
    CompanyFinancial,
    CompanyRatio,
    Disclosure,
)


def EncodeCompanySnapshot(snapshot: CompanyEvidenceSnapshot) -> str:
    return json.dumps(
        _snapshot_fields(snapshot),
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def EncodeCompanyPublication(snapshot: CompanyEvidenceSnapshot) -> tuple[str, str]:
    """Project once; hash the existing normalized protocol before adding publication coordinates."""
    payload = _snapshot_fields(snapshot)
    payload["version"] = ""
    payload["published_at"] = datetime.combine(
        snapshot.window_end, datetime.min.time(), snapshot.published_at.tzinfo
    ).isoformat()
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    payload["version"] = digest
    payload["published_at"] = snapshot.published_at.isoformat()
    return digest, json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _snapshot_fields(snapshot: CompanyEvidenceSnapshot) -> dict[str, object]:
    return {
        "code": snapshot.code,
        "published_at": snapshot.published_at.isoformat(),
        "window_start": snapshot.window_start.isoformat(),
        "window_end": snapshot.window_end.isoformat(),
        "version": snapshot.version,
        "announcements": [
            {"disclosure": _disclosure(item.disclosure), "text": item.text, "checked": item.checked}
            for item in snapshot.announcements
        ],
        "announcement_complete": snapshot.announcement_complete,
        "financials": [_financial(item) for item in snapshot.financials],
        "pledge": _ratio(snapshot.pledge) if snapshot.pledge else None,
        "unlocks": [_ratio(item) for item in snapshot.unlocks],
        "unlock_checked": snapshot.unlock_checked,
        "news": [_disclosure(item) for item in snapshot.news],
        "news_checked": snapshot.news_checked,
        "failures": snapshot.failures,
    }


def DecodeCompanySnapshot(payload: str) -> CompanyEvidenceSnapshot:
    try:
        snapshot = _decode(payload)
    except (KeyError, TypeError, AttributeError, OverflowError) as exc:
        raise ValueError("company_snapshot_schema_invalid") from exc
    if CompanySnapshotDigest(snapshot) != snapshot.version:
        raise ValueError("company_snapshot_digest_mismatch")
    return snapshot


def CompanySnapshotDigest(snapshot: CompanyEvidenceSnapshot) -> str:
    normalized = replace(
        snapshot,
        version="",
        published_at=datetime.combine(snapshot.window_end, datetime.min.time(), snapshot.published_at.tzinfo),
    )
    return hashlib.sha256(EncodeCompanySnapshot(normalized).encode()).hexdigest()


def _decode(payload: str) -> CompanyEvidenceSnapshot:
    raw = json.loads(payload)
    return CompanyEvidenceSnapshot(
        raw["code"],
        datetime.fromisoformat(raw["published_at"]),
        date.fromisoformat(raw["window_start"]),
        date.fromisoformat(raw["window_end"]),
        raw["version"],
        tuple(
            CompanyDocument(_decode_disclosure(item["disclosure"]), item["text"], item["checked"])
            for item in raw["announcements"]
        ),
        raw["announcement_complete"],
        tuple(
            CompanyFinancial(
                item["code"],
                date.fromisoformat(item["report_date"]),
                datetime.fromisoformat(item["published_at"]),
                *(item[field] for field in _FINANCIAL_FIELDS),
            )
            for item in raw["financials"]
        ),
        _decode_ratio(raw["pledge"]) if raw["pledge"] is not None else None,
        tuple(_decode_ratio(item) for item in raw["unlocks"]),
        raw["unlock_checked"],
        tuple(_decode_disclosure(item) for item in raw["news"]),
        raw["news_checked"],
        tuple(raw["failures"]),
    )


_FINANCIAL_FIELDS = (
    "basic_eps",
    "book_value_per_share",
    "revenue_growth_pct",
    "net_profit_growth_pct",
    "core_profit_growth_pct",
    "roe_pct",
    "parent_net_profit",
    "core_net_profit",
)


def _financial(item: CompanyFinancial) -> dict[str, object]:
    return {
        "code": item.code,
        "report_date": item.report_date.isoformat(),
        "published_at": item.published_at.isoformat(),
        "basic_eps": item.basic_eps,
        "book_value_per_share": item.book_value_per_share,
        "revenue_growth_pct": item.revenue_growth_pct,
        "net_profit_growth_pct": item.net_profit_growth_pct,
        "core_profit_growth_pct": item.core_profit_growth_pct,
        "roe_pct": item.roe_pct,
        "parent_net_profit": item.parent_net_profit,
        "core_net_profit": item.core_net_profit,
    }


def _disclosure(item: Disclosure) -> dict[str, object]:
    return {
        "source": item.source,
        "identity": item.identity,
        "codes": item.codes,
        "title": item.title,
        "published_at": item.published_at.isoformat(),
        "url": item.url,
        "digest": item.digest,
        "kind": item.kind,
    }


def _decode_disclosure(raw: dict[str, object]) -> Disclosure:
    return Disclosure(
        str(raw["source"]),
        str(raw["identity"]),
        tuple(cast(list[str], raw["codes"])),
        str(raw["title"]),
        datetime.fromisoformat(str(raw["published_at"])),
        str(raw["url"]),
        str(raw["digest"]),
        cast(Literal["announcement", "news"], raw["kind"]),
    )


def _ratio(item: CompanyRatio) -> dict[str, object]:
    return {"code": item.code, "day": item.day.isoformat(), "ratio_pct": item.ratio_pct}


def _decode_ratio(raw: dict[str, object]) -> CompanyRatio:
    return CompanyRatio(str(raw["code"]), date.fromisoformat(str(raw["day"])), float(cast(float, raw["ratio_pct"])))


__all__ = ["CompanySnapshotDigest", "DecodeCompanySnapshot", "EncodeCompanySnapshot", "EncodeCompanyPublication"]

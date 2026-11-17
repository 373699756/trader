"""Explicit synthetic normalized inputs shared by isolated persistence measurements."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from trader.download.domain.company_descriptions import CompanyRiskDescription
from trader.download.domain.company_events import (
    CompanyDataState, CompanyEventStatus, CompanyRiskEvent, CompanyRiskType,
    CompanySeverityBasis, CompanySubject,
)
from trader.download.domain.company_facts import CompanyFacts
from trader.download.infra.company_facts_index import CompanyInputReceipt
from trader.infra.serialization.canonical_codec import CanonicalDigest


@dataclass(frozen=True)
class CompanyMeasurementInputs:
    facts: tuple[CompanyFacts, ...]
    receipts: tuple[CompanyInputReceipt, ...]
    sources: tuple[tuple[str, str], ...]


def BuildCompanyMeasurementInputs(
    codes: tuple[str, ...], at: datetime, extraction_hash: bytes, *, include_risks: bool = False,
) -> CompanyMeasurementInputs:
    """Build three synthetic evidence receipts per issuer without parsing or fetching."""
    if at.utcoffset() is None or len(set(codes)) != len(codes):
        raise ValueError("company_measurement_scope_invalid")
    facts: list[CompanyFacts] = []
    receipts: list[CompanyInputReceipt] = []
    sources: list[tuple[str, str]] = []
    body_hash = hashlib.sha256(b"isolated synthetic normalized company evidence").digest()
    for code in codes:
        keys = tuple(f"measurement:{code}:{offset}" for offset in range(3))
        for key in keys:
            sources.append((key, "cninfo"))
            receipts.append(CompanyInputReceipt(
                "cninfo", key, code, CanonicalDigest((code, key, at)), extraction_hash, body_hash, at,
            ))
        risks = (CompanyRiskEvent(
            f"measurement-risk:{code}", CompanyRiskType.OFFICIAL_INVESTIGATION, 4,
            CompanySubject.ISSUER, CompanySeverityBasis.HARD, None, at, at, at,
            CompanyEventStatus.ONGOING, None, None, CompanyRiskDescription(CompanyRiskType.OFFICIAL_INVESTIGATION), keys,
        ),) if include_risks else ()
        facts.append(CompanyFacts(
            code, None, CompanyDataState.UNAVAILABLE, None, CompanyDataState.UNAVAILABLE,
            (), None, None, CompanyDataState.UNAVAILABLE, risks, (), (), 0,
            at.date(), at.date(), CompanyDataState.COMPLETE, at, at, latest_financial=None,
        ))
    return CompanyMeasurementInputs(tuple(facts), tuple(receipts), tuple(sources))

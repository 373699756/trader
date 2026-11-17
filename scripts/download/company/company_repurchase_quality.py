"""Compare frozen independent statement annotations with current pure field extraction."""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

from trader.download.domain.company_event_extraction import ExtractCompanyFieldEvidence, ExtractCompanyLinkCandidate
from trader.download.domain.company_events import CompanyEventPolicy, CompanyPositiveStatus, RepurchaseMeasurements
from trader.download.domain.company_evidence import CompanyDocument, CompanySyncConfiguration, Disclosure
from trader.download.domain.company_field_results import CompanyFieldConclusion
from trader.download.infra.company_document_files import CompanyDocumentFiles
from trader.download.infra.company_field_publication import CompanyFieldProofRulesHash
from trader.infra.regular_file import OpenRegularFile


@dataclass(frozen=True, slots=True)
class _Expected:
    key: tuple[str, str, str]
    verdict: str
    value: float | str | None
    regions: tuple[tuple[int, int], ...]


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("company_quality_object_invalid")
    return cast(dict[str, object], value)


def _items(payload: dict[str, object]) -> tuple[dict[str, object], ...]:
    entries = payload.get("items")
    if not isinstance(entries, list) or not 1 <= len(entries) <= 19:
        raise ValueError("company_quality_items_invalid")
    return tuple(_object(item) for item in entries)


def _text(row: dict[str, object], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise ValueError("company_quality_text_invalid")
    return value


def _key(row: dict[str, object]) -> tuple[str, str, str]:
    return _text(row, "code"), _text(row, "event_id"), _text(row, "field")


def _expectation(row: dict[str, object], regions: dict[str, object]) -> _Expected:
    value, lines = row.get("expected"), row.get("lines")
    if (value is not None and not isinstance(value, (str, float))
        or isinstance(value, float) and not math.isfinite(value)
        or not isinstance(lines, list) or not lines or any(type(line) is not int or line < 1 for line in lines)):
        raise ValueError("company_quality_annotation_invalid")
    verdict = _text(row, "verdict")
    if verdict not in {"prove_current_statement", "withdraw_disposal_measurement", "reject_conflicting_original"}:
        raise ValueError("company_quality_verdict_invalid")
    raw_regions = regions.get(_key(row)[1], [])
    if not isinstance(raw_regions, list):
        raise ValueError("company_quality_regions_invalid")
    parsed: list[tuple[int, int]] = []
    for region in raw_regions:
        if (not isinstance(region, list) or len(region) != 2
            or any(type(line) is not int for line in region) or not 1 <= region[0] <= region[1]):
            raise ValueError("company_quality_regions_invalid")
        parsed.append((region[0], region[1]))
    if verdict == "prove_current_statement" and not parsed:
        raise ValueError("company_quality_regions_missing")
    return _Expected(_key(row), verdict, value, tuple(parsed))


def AuditCompanyRepurchaseQuality(
    targets_path: Path,
    annotations_path: Path,
    *,
    observed_at: datetime,
    event_policy: CompanyEventPolicy,
    configuration: CompanySyncConfiguration,
    budget_seconds: float,
) -> dict[str, object]:
    """Read exact originals without a database, client, publication or source-age change."""
    if observed_at.utcoffset() is None or not math.isfinite(budget_seconds) or not 0 < budget_seconds <= 600:
        raise ValueError("company_quality_budget_invalid")
    deadline = time.monotonic() + budget_seconds
    def check() -> None:
        if time.monotonic() >= deadline:
            raise TimeoutError("company_quality_deadline_exceeded")
    with OpenRegularFile(targets_path, maximum_bytes=256 * 1024, check=check) as stream:
        target_bytes = stream.read()
    with OpenRegularFile(annotations_path, maximum_bytes=256 * 1024, check=check) as stream:
        annotation_bytes = stream.read()
    targets, annotations = _object(json.loads(target_bytes)), _object(json.loads(annotation_bytes))
    target_hash = hashlib.sha256(target_bytes).hexdigest()
    if annotations.get("targets_sha256") != target_hash:
        raise ValueError("company_quality_annotation_scope_changed")
    regions = _object(annotations.get("proof_regions"))
    expected = tuple(_expectation(row, regions) for row in _items(annotations))
    rows = _items(targets)
    if (len({_key(row) for row in rows}) != len(rows) or len({item.key for item in expected}) != len(expected)
        or {_key(row) for row in rows} != {item.key for item in expected}):
        raise ValueError("company_quality_population_mismatch")
    results: list[dict[str, object]] = []
    for row in rows:
        check()
        expectation = next(item for item in expected if item.key == _key(row))
        documents = CompanyDocumentFiles(
            Path(_text(row, "text_root")), max_text_characters=configuration.extended_document_limits.max_text_characters,
            max_bytes=configuration.temporary_text_capacity_bytes, reserve_bytes=configuration.temporary_disk_reserve_bytes,
        )
        body_hash = bytes.fromhex(_text(row, "body_hash"))
        text = documents.read(body_hash, check=check)
        document = CompanyDocument(Disclosure(
            _text(row, "source"), _text(row, "source_key"), (expectation.key[0],), _text(row, "title"),
            datetime.fromisoformat(_text(row, "published_at")), _text(row, "url"), _text(row, "record_hash"),
        ), text, True)
        fields = ExtractCompanyFieldEvidence(document, observed_at, event_policy)
        field: CompanyFieldConclusion | None = next((item for item in fields if item.field == expectation.key[2]), None)
        candidate = ExtractCompanyLinkCandidate(document, observed_at, event_policy)
        event = candidate.positive if candidate is not None else None
        if expectation.verdict == "prove_current_statement":
            matched = (field is not None and field.state == "verified" and field.value == expectation.value
                       and field.evidence[0].subject.value == "issuer"
                       and any(start <= field.evidence[0].start_line <= field.evidence[0].end_line <= end
                               for start, end in expectation.regions))
        elif expectation.verdict == "withdraw_disposal_measurement":
            matched = (field is None and event is not None and isinstance(event.measurements, RepurchaseMeasurements)
                       and event.measurements.actual_ratio_pct is None and event.status is CompanyPositiveStatus.PLANNED
                       and event.positive_level == 0 and not event.level_milestones)
        else:
            matched = field is None
        results.append({
            "code": expectation.key[0], "event_id": expectation.key[1], "field": expectation.key[2],
            "expected_verdict": expectation.verdict, "expected_value": expectation.value,
            "actual_value": field.value if field else None, "annotation_matched": matched,
            "source_key": document.disclosure.identity, "record_hash": document.disclosure.digest,
            "body_hash": body_hash.hex(), "published_at": document.disclosure.published_at.isoformat(),
            "actual_source_event_id": field.event_id if field else None,
            "actual_status": event.status.value if event else None,
            "proof_start_line": field.evidence[0].start_line if field else None,
            "proof_end_line": field.evidence[0].end_line if field else None,
            "formal_event_binding_verified": False,
        })
    return {
        "scope": "independent_annotations_for_frozen_repurchase_statements", "status": "passed" if all(
            row["annotation_matched"] for row in results) else "degraded",
        "source_publication_hash": targets.get("source_publication_hash"), "target_manifest_sha256": target_hash,
        "annotations_sha256": hashlib.sha256(annotation_bytes).hexdigest(), "field_rules_hash": CompanyFieldProofRulesHash().hex(),
        "checked_targets": len(results), "annotation_matches": sum(row["annotation_matched"] is True for row in results),
        "proved_statements": sum(row["expected_verdict"] == "prove_current_statement" and row["annotation_matched"] is True for row in results),
        "source_data_writes": 0, "network_requests": 0, "formal_event_binding_verified": False,
        "field_correctness_verified": False, "full_market_verified": False, "production_adopted": False, "items": results,
    }


__all__ = ["AuditCompanyRepurchaseQuality"]

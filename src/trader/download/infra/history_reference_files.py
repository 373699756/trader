"""Strict codecs for file-backed history eligibility and dated industry references."""

from __future__ import annotations

import json
from bisect import bisect_right
from collections import defaultdict
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Literal, cast

from trader.download.domain.baostock_daily import BaoStockIndustryInterval
from trader.download.domain.history_reference import HistoryReferenceSnapshot, HistoryStEvidence
from trader.download.domain.history_revision import HistoryRevision
from trader.infra.atomic_files.json import atomic_read_json, atomic_write_json


def history_filter_config_root(history_root: Path) -> Path:
    """Return the sole root for human-readable history filter configuration."""

    return history_root.parent / "filter_config"


def history_st_evidence_path(history_root: Path) -> Path:
    return history_filter_config_root(history_root) / "historical_st.json"


def history_industry_mapping_path(history_root: Path) -> Path:
    return history_filter_config_root(history_root) / "industry_mapping.json"


class HistoryReferenceIndex:
    def __init__(self, reference: HistoryReferenceSnapshot) -> None:
        self.reference = reference
        self._eligible = frozenset(reference.eligible_codes)
        grouped: dict[str, list[BaoStockIndustryInterval]] = defaultdict(list)
        for item in reference.industry_intervals:
            grouped[item.code].append(item)
        self._intervals = {code: tuple(values) for code, values in grouped.items()}
        self._starts = {code: tuple(item.effective_from for item in values) for code, values in self._intervals.items()}

    def apply(self, revision: HistoryRevision) -> HistoryRevision:
        industry = self.industry_on(revision.code, revision.trade_date)
        return replace(
            revision,
            is_st=False if revision.code in self._eligible else None,
            industry=industry.industry if industry is not None else None,
            industry_classification=industry.classification if industry is not None else None,
        )

    def eligible(self, code: str) -> bool:
        return code in self._eligible

    def industry_on(self, code: str, day: date) -> BaoStockIndustryInterval | None:
        values = self._intervals.get(code, ())
        position = bisect_right(self._starts.get(code, ()), day) - 1
        industry = values[position] if position >= 0 else None
        if industry is not None and industry.effective_to is not None and day >= industry.effective_to:
            industry = None
        return industry


def write_history_reference(path: Path, reference: HistoryReferenceSnapshot) -> None:
    industries: dict[str, list[list[str | None]]] = defaultdict(list)
    for item in reference.industry_intervals:
        industries[item.code].append(
            [
                item.effective_from.isoformat(),
                item.effective_to.isoformat() if item.effective_to is not None else None,
                item.industry,
                item.classification,
            ]
        )
    atomic_write_json(
        path,
        {
            "content_hash": reference.content_hash,
            "eligible_codes": reference.eligible_codes,
            "st_statuses": {item.code: [item.checked_on.isoformat(), item.status] for item in reference.st_evidence},
            "industries": industries,
        },
    )


def read_history_reference(path: Path) -> HistoryReferenceSnapshot:
    try:
        payload = atomic_read_json(path)
        if not isinstance(payload, dict) or set(payload) != {
            "content_hash",
            "eligible_codes",
            "st_statuses",
            "industries",
        }:
            raise ValueError("history reference fields are invalid")
        codes = payload["eligible_codes"]
        intervals = payload["industries"]
        if not isinstance(codes, list) or not all(isinstance(code, str) for code in codes):
            raise ValueError("history reference codes are invalid")
        if not isinstance(intervals, dict):
            raise ValueError("history industry intervals are invalid")
        reference = HistoryReferenceSnapshot(
            tuple(codes),
            tuple(
                _decode_industry(code, item)
                for code, items in intervals.items()
                if isinstance(code, str) and isinstance(items, list)
                for item in items
            ),
            _decode_st_rows(payload["st_statuses"]),
        )
        if reference.content_hash != payload["content_hash"]:
            raise ValueError("history reference hash is invalid")
        return reference
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("history reference file is invalid") from exc


def read_bound_history_reference(root: Path, supplier_contract: str) -> HistoryReferenceSnapshot:
    if not supplier_contract.startswith("history_ref_"):
        raise ValueError("history reference contract requires rebuilding history")
    digest = supplier_contract[len("history_ref_") : len("history_ref_") + 64]
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("history reference identity is invalid")
    reference = read_history_reference(history_industry_mapping_path(root))
    if reference.content_hash != digest:
        raise ValueError("history reference identity conflict")
    return reference


def read_st_evidence(path: Path) -> tuple[HistoryStEvidence, ...]:
    if not path.is_file():
        return ()
    return _decode_st_rows(atomic_read_json(path))


def write_st_evidence(path: Path, evidence: tuple[HistoryStEvidence, ...]) -> None:
    atomic_write_json(path, {item.code: [item.checked_on.isoformat(), item.status] for item in evidence})


def _decode_st_rows(value: object) -> tuple[HistoryStEvidence, ...]:
    if not isinstance(value, dict):
        raise ValueError("history ST evidence rows are invalid")
    evidence = []
    for code, item in value.items():
        if (
            not isinstance(code, str)
            or not isinstance(item, list)
            or len(item) != 2
            or not all(isinstance(text, str) for text in item)
        ):
            raise ValueError("history ST evidence row is invalid")
        status = cast(Literal["ever_st", "clear", "unknown"], item[1])
        evidence.append(HistoryStEvidence(code, date.fromisoformat(item[0]), status))
    if len({item.code for item in evidence}) != len(evidence):
        raise ValueError("history ST evidence codes are duplicated")
    return tuple(evidence)


def _decode_industry(code: str, item: object) -> BaoStockIndustryInterval:
    if not isinstance(item, list) or len(item) != 4:
        raise ValueError("history industry row is invalid")
    start, end, industry, classification = item
    if not isinstance(start, str) or not isinstance(industry, str) or not isinstance(classification, str):
        raise ValueError("history industry text is invalid")
    if end is not None and not isinstance(end, str):
        raise ValueError("history industry end date is invalid")
    return BaoStockIndustryInterval(
        code,
        date.fromisoformat(start),
        date.fromisoformat(end) if end else None,
        industry,
        classification,
    )


__all__ = [
    "HistoryReferenceIndex",
    "history_filter_config_root",
    "history_industry_mapping_path",
    "history_st_evidence_path",
    "read_bound_history_reference",
    "read_history_reference",
    "read_st_evidence",
    "write_history_reference",
    "write_st_evidence",
]

"""Strict codecs for file-backed history eligibility and dated industry references."""

from __future__ import annotations

import json
from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Collection
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Literal, cast

from trader.download.domain.baostock_daily import BaoStockIndustryInterval
from trader.download.domain.history_reference import HistoryReferenceSnapshot, HistoryStEvidence
from trader.download.domain.history_revision import HistoryRevision
from trader.infra.atomic_files.json import AtomicReadJson, AtomicWriteJson


def HistoryStEvidencePath(reference_root: Path) -> Path:
    return reference_root / "historical_st.json"


def HistoryReferencePath(history_root: Path, digest: str) -> Path:
    return history_root / "references" / f"{digest}.json"


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

    def industry_sample_upper_bounds(
        self, codes: Collection[str], dates: tuple[date, ...], required_sessions: int, anchors: frozenset[date]
    ) -> tuple[tuple[str, int], ...]:
        """Conservative upper bounds: price, listing and interior gaps can only reduce them."""
        if len(dates) < required_sessions:
            return ()
        allowed = tuple(day for day in dates[required_sessions - 1 :] if day in anchors)
        counts: dict[str, int] = defaultdict(int)
        for code in codes:
            if code not in self._eligible:
                continue
            for interval in self._intervals.get(code, ()):
                start = bisect_left(allowed, interval.effective_from)
                end = bisect_left(allowed, interval.effective_to) if interval.effective_to else len(allowed)
                counts[interval.industry] += max(0, end - start)
        return tuple(sorted(counts.items()))

    def has_industry_window(
        self,
        codes: Collection[str],
        calendar_dates: tuple[date, ...],
        required_sessions: int,
        *,
        anchor_dates: frozenset[date] | None = None,
    ) -> bool:
        """Return whether any eligible code has one continuous dated-industry window."""

        dates = tuple(calendar_dates)
        if not dates or dates != tuple(sorted(set(dates))) or required_sessions < 1 or required_sessions > len(dates):
            return False
        anchors = tuple(index for index, day in enumerate(dates) if anchor_dates is None or day in anchor_dates)
        for code in codes:
            if code not in self._eligible:
                continue
            covered_start: int | None = None
            covered_end = 0
            for interval in self._intervals.get(code, ()):
                start = bisect_left(dates, interval.effective_from)
                end = bisect_left(dates, interval.effective_to) if interval.effective_to is not None else len(dates)
                if end <= start:
                    continue
                if covered_start is None or start > covered_end:
                    covered_start = start
                    covered_end = end
                else:
                    covered_end = max(covered_end, end)
                position = bisect_left(anchors, covered_start + required_sessions - 1)
                if position < len(anchors) and anchors[position] < covered_end:
                    return True
        return False


def WriteHistoryReference(path: Path, reference: HistoryReferenceSnapshot) -> None:
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
    AtomicWriteJson(
        path,
        {
            "content_hash": reference.content_hash,
            "eligible_codes": reference.eligible_codes,
            "st_statuses": {item.code: [item.checked_on.isoformat(), item.status] for item in reference.st_evidence},
            "industries": industries,
        },
    )


def ReadHistoryReference(path: Path) -> HistoryReferenceSnapshot:
    try:
        payload = AtomicReadJson(path)
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


def ReadBoundHistoryReference(root: Path, supplier_contract: str) -> HistoryReferenceSnapshot:
    if not supplier_contract.startswith("history_ref_"):
        raise ValueError("history reference contract requires rebuilding history")
    digest = supplier_contract[len("history_ref_") : len("history_ref_") + 64]
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("history reference identity is invalid")
    reference = ReadHistoryReference(HistoryReferencePath(root, digest))
    if reference.content_hash != digest:
        raise ValueError("history reference identity conflict")
    return reference


def ReadStEvidence(path: Path) -> tuple[HistoryStEvidence, ...]:
    if not path.is_file():
        return ()
    return _decode_st_rows(AtomicReadJson(path))


def WriteStEvidence(path: Path, evidence: tuple[HistoryStEvidence, ...]) -> None:
    AtomicWriteJson(path, {item.code: [item.checked_on.isoformat(), item.status] for item in evidence})


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
    "HistoryStEvidencePath",
    "ReadBoundHistoryReference",
    "ReadHistoryReference",
    "ReadStEvidence",
    "WriteHistoryReference",
    "WriteStEvidence",
]

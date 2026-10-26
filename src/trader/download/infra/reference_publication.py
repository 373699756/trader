"""Atomic publication of the complete manual reference input."""

from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Literal, cast
from zoneinfo import ZoneInfo

from trader.download.domain.baostock_daily import BaoStockBoard, BaoStockSecurity
from trader.download.domain.company_evidence import EvidenceSyncProgress
from trader.download.domain.history_reference import HistoryStEvidence
from trader.download.domain.prepared_reference import PreparedReference
from trader.infra.atomic_files.json import AtomicReadJson, AtomicWriteJson


class ReferencePublication:
    def __init__(self, reference_root: Path) -> None:
        self._path = reference_root / "inputs.json"

    def calendar(
        self,
        checked_on: date,
        *,
        fetch: Callable[[], tuple[date, ...]],
        progress: Callable[[EvidenceSyncProgress], None] | None,
    ) -> tuple[date, ...]:
        failure = ""
        try:
            reference = self.read()
        except (OSError, ValueError):
            reference = None
            failure = "reference_cache_invalid"
        reused = reference is not None and reference.checked_at.date() == checked_on
        if progress is not None:
            progress(
                EvidenceSyncProgress(
                    "calendar",
                    int(reused),
                    0 if reused else None,
                    0,
                    0,
                    int(reused),
                    failure,
                    status="reused" if reused else "running",
                )
            )
        if reused and reference is not None:
            return reference.open_dates
        calendar = fetch()
        if progress is not None:
            progress(EvidenceSyncProgress("calendar", 1, 0, 0, 0, 0, status="complete"))
        return calendar

    def publish(self, reference: PreparedReference) -> None:
        AtomicWriteJson(
            self._path,
            {
                "schema": "prepared_reference",
                "identity": reference.identity,
                "checked_at": reference.checked_at.isoformat(),
                "industry_version": reference.industry_version,
                "open_dates": [day.isoformat() for day in reference.open_dates],
                "official_universe": [
                    [
                        item.code,
                        item.name,
                        item.board,
                        item.listed_on.isoformat(),
                        item.delisted_on.isoformat() if item.delisted_on else None,
                        item.source_version,
                    ]
                    for item in reference.official_universe
                ],
                "st_evidence": [
                    [item.code, item.checked_on.isoformat(), item.status] for item in reference.st_evidence
                ],
            },
        )

    def read(self) -> PreparedReference | None:
        if not self._path.exists():
            return None
        if self._path.stat().st_size > 8 * 1024**2:
            raise ValueError("reference_publication_oversized")
        raw = AtomicReadJson(self._path)
        if (
            not isinstance(raw, dict)
            or set(raw)
            != {
                "schema",
                "identity",
                "checked_at",
                "open_dates",
                "official_universe",
                "st_evidence",
                "industry_version",
            }
            or raw["schema"] != "prepared_reference"
        ):
            raise ValueError("reference_publication_schema_invalid")
        reference = PreparedReference(
            tuple(_security(row) for row in _rows(raw["official_universe"])),
            tuple(_evidence(row) for row in _rows(raw["st_evidence"])),
            _checked_at(raw["checked_at"]),
            tuple(date.fromisoformat(_text(value)) for value in _rows(raw["open_dates"])),
            _text(raw["industry_version"]) if raw["industry_version"] is not None else None,
        )
        if raw["identity"] != reference.identity:
            raise ValueError("reference_publication_hash_conflict")
        return reference

    def identity(self) -> str | None:
        if not self._path.exists():
            return None
        if self._path.stat().st_size > 8 * 1024**2:
            raise ValueError("reference_publication_oversized")
        raw = AtomicReadJson(self._path)
        if not isinstance(raw, dict) or raw.get("schema") != "prepared_reference":
            raise ValueError("reference_publication_schema_invalid")
        identity = _text(raw.get("identity"))
        if len(identity) != 64 or any(character not in "0123456789abcdef" for character in identity):
            raise ValueError("reference_publication_identity_invalid")
        return identity


def _checked_at(value: object) -> datetime:
    parsed = datetime.fromisoformat(_text(value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("reference_publication_timezone_missing")
    return parsed.astimezone(ZoneInfo("Asia/Shanghai"))


def _rows(value: object) -> list[object]:
    if not isinstance(value, list) or len(value) > 20_000:
        raise ValueError("reference_publication_list_invalid")
    return value


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("reference_publication_text_invalid")
    return value


def _security(value: object) -> BaoStockSecurity:
    row = _rows(value)
    if len(row) != 6:
        raise ValueError("reference_security_invalid")
    return BaoStockSecurity(
        _text(row[0]),
        _text(row[1]),
        cast(BaoStockBoard, _text(row[2])),
        date.fromisoformat(_text(row[3])),
        date.fromisoformat(_text(row[4])) if row[4] is not None else None,
        _text(row[5]),
    )


def _evidence(value: object) -> HistoryStEvidence:
    row = _rows(value)
    if len(row) != 3:
        raise ValueError("reference_st_invalid")
    return HistoryStEvidence(
        _text(row[0]),
        date.fromisoformat(_text(row[1])),
        cast(Literal["ever_st", "clear", "unknown"], _text(row[2])),
    )


__all__ = ["ReferencePublication"]

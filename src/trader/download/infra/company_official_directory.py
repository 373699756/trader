"""Decode the observed official SZSE annList protocol without promoting metadata to facts."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import cast
from zoneinfo import ZoneInfo

from trader.download.domain.company_evidence import EvidenceMarket
from trader.download.domain.company_source_records import (
    CompanyOfficialDirectoryObservation,
    CompanyOfficialDirectoryPage,
    CompanyOfficialDirectoryQuery,
)

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_ZONE = ZoneInfo("Asia/Shanghai")


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("company_official_directory_object_invalid")
    return cast(dict[str, object], value)


def _text(row: dict[str, object], key: str, maximum: int) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError("company_official_directory_field_invalid")
    return value


def DecodeCompanyOfficialDirectory(
    content: bytes, query: CompanyOfficialDirectoryQuery, *, observed_at: datetime,
) -> CompanyOfficialDirectoryPage:
    """No client, file access or inference of report period, revision, case state or body."""
    if len(content) > 1024 * 1024 or observed_at.utcoffset() is None or query.end_day > observed_at.astimezone(_ZONE).date():
        raise ValueError("company_official_directory_boundary_invalid")
    payload = _object(json.loads(content))
    total, rows = payload.get("announceCount"), payload.get("data")
    if (type(total) is not int or not 0 <= total <= 1000000 or not isinstance(rows, list)
        or len(rows) != min(query.page_size, max(0, total - (query.page - 1) * query.page_size))
        or total and (query.page - 1) * query.page_size >= total or not total and query.page != 1):
        raise ValueError("company_official_directory_count_invalid")
    observations: list[CompanyOfficialDirectoryObservation] = []
    for entry in rows:
        row = _object(entry)
        identity, announcement_id = _text(row, "id", 36), row.get("annId")
        raw_codes = row.get("secCode")
        if (re.fullmatch(_UUID, identity) is None or type(announcement_id) is not int or announcement_id <= 0
            or not isinstance(raw_codes, list) or not 1 <= len(raw_codes) <= 32
            or any(not isinstance(code, str) or EvidenceMarket(code) != "szse" for code in raw_codes)
            or len(set(raw_codes)) != len(raw_codes)):
            raise ValueError("company_official_directory_identity_invalid")
        published = datetime.strptime(_text(row, "publishTime", 19), "%Y-%m-%d %H:%M:%S").replace(tzinfo=_ZONE)
        if not query.start_day <= published.date() <= query.end_day or published > observed_at:
            raise ValueError("company_official_directory_time_invalid")
        path, format_name = _text(row, "attachPath", 512), _text(row, "attachFormat", 16)
        if (re.fullmatch(rf"/disc/disk\d{{2}}/finalpage/\d{{4}}-\d{{2}}-\d{{2}}/{_UUID}\.(?:pdf|PDF)", path) is None
            or format_name != "PDF"):
            raise ValueError("company_official_directory_attachment_unrecognized")
        observations.append(CompanyOfficialDirectoryObservation(
            identity, announcement_id, tuple(cast(list[str], raw_codes)), _text(row, "title", 1024), published, path, format_name,
        ))
    if (len({item.identity for item in observations}) != len(observations)
        or len({item.announcement_id for item in observations}) != len(observations)):
        raise ValueError("company_official_directory_duplicate_identity")
    return CompanyOfficialDirectoryPage(query, total, tuple(observations), hashlib.sha256(content).hexdigest())


__all__ = ["DecodeCompanyOfficialDirectory"]

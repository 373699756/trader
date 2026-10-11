"""Local-only resumable progress for qfq downloads."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from trader.infra.atomic_files.json import atomic_write_json


class QfqCheckpoint:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._day: str | None = None
        self._source: str | None = None
        self._codes: set[str] = set()
        if path.is_file():
            try:
                value: object = json.loads(path.read_text(encoding="ascii"))
                if (
                    isinstance(value, dict)
                    and isinstance(value.get("day"), str)
                    and isinstance(value.get("codes"), list)
                    and isinstance(value.get("source"), str)
                ):
                    self._day = value["day"]
                    self._source = value["source"]
                    self._codes = {
                        code for code in value["codes"] if isinstance(code, str) and len(code) == 6 and code.isdigit()
                    }
            except (OSError, ValueError, TypeError):
                self._day = None

    def completed(self, day: date, code: str, source: str) -> bool:
        return self._day == day.isoformat() and self._source == source and code in self._codes

    def confirm_many(self, day: date, codes: tuple[str, ...], source: str) -> None:
        if not codes:
            return
        if any(len(code) != 6 or not code.isascii() or not code.isdigit() for code in codes):
            raise ValueError("qfq checkpoint codes invalid")
        next_day = day.isoformat()
        next_codes = set() if self._day != next_day or self._source != source else set(self._codes)
        next_codes.update(codes)
        atomic_write_json(self._path, {"day": next_day, "source": source, "codes": sorted(next_codes)})
        self._day = next_day
        self._source = source
        self._codes = next_codes

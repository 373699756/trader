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
        self._codes: set[str] = set()
        if path.is_file():
            try:
                value: object = json.loads(path.read_text(encoding="ascii"))
                if (
                    isinstance(value, dict)
                    and isinstance(value.get("day"), str)
                    and isinstance(value.get("codes"), list)
                ):
                    self._day = value["day"]
                    self._codes = {
                        code for code in value["codes"] if isinstance(code, str) and len(code) == 6 and code.isdigit()
                    }
            except (OSError, ValueError, TypeError):
                self._day = None

    def completed(self, day: date, code: str) -> bool:
        return self._day == day.isoformat() and code in self._codes

    def confirm(self, day: date, code: str) -> None:
        next_day = day.isoformat()
        next_codes = set() if self._day != next_day else set(self._codes)
        next_codes.add(code)
        atomic_write_json(self._path, {"day": next_day, "codes": sorted(next_codes)})
        self._day = next_day
        self._codes = next_codes

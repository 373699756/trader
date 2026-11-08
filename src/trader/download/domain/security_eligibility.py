"""Pure security-name eligibility shared by download paths."""

from __future__ import annotations

import re
import unicodedata

_ST_NAME = re.compile(r"(?:^|[^A-Za-z])(?:S\*?ST|\*?ST)", re.IGNORECASE)
_MISSING_NAME_HISTORY = frozenset({"", "-", "--", "暂无", "暂无数据"})


def IsMissingNameHistory(name_history: str) -> bool:
    """Recognize unavailable parsed history; a current name cannot fill this gap."""

    return unicodedata.normalize("NFKC", name_history).strip() in _MISSING_NAME_HISTORY


def IsStSecurityName(name: str) -> bool:
    """Return whether an official current or historical name carries an ST marker."""

    normalized = unicodedata.normalize("NFKC", name).strip()
    return bool(normalized and _ST_NAME.search(normalized))


__all__ = [
    "IsMissingNameHistory",
    "IsStSecurityName",
]

"""Pure security-name eligibility shared by download paths."""

from __future__ import annotations

import re
import unicodedata

_ST_NAME = re.compile(r"(?:^|[^A-Za-z])(?:S\*?ST|\*?ST)", re.IGNORECASE)


def is_st_security_name(name: str) -> bool:
    """Return whether an official current or historical name carries an ST marker."""

    normalized = unicodedata.normalize("NFKC", name).strip()
    return bool(normalized and _ST_NAME.search(normalized))


__all__ = ["is_st_security_name"]

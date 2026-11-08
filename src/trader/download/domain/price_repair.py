"""Immutable unresolved daily price observations."""

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class PriceIssue:
    code: str
    dates: tuple[date, ...]
    reason: str


def ValidateRepairSelection(codes: frozenset[str] | None, *, fill: bool, target: str) -> None:
    """Validate an explicit bounded repair scope before supplier or database activity."""
    if fill and target == "all":
        raise ValueError("fill_requires_explicit_target")
    if codes is None:
        return
    if (
        not fill or target not in {"history", "market"} or type(codes) is not frozenset
        or not 1 <= len(codes) <= 5
        or any(not isinstance(code, str) or len(code) != 6 or not code.isascii() or not code.isdigit() for code in codes)
    ):
        raise ValueError("repair_codes_require_history_or_market_fill_and_1_to_5_codes")

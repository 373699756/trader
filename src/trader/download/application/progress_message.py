"""Immutable, terminal-independent download feedback and display-cell layout."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum


class ProgressLevel(Enum):
    NORMAL = "normal"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


def HighestProgressLevel(*levels: ProgressLevel) -> ProgressLevel:
    order = tuple(ProgressLevel)
    return max(levels, key=order.index, default=ProgressLevel.NORMAL)


@dataclass(frozen=True)
class ProgressField:
    text: str
    level: ProgressLevel = ProgressLevel.NORMAL


@dataclass(frozen=True)
class ProgressMessage:
    fields: tuple[ProgressField, ...]
    stock_failure_code: str | None = None


def CleanProgressText(text: str) -> str:
    # Remove complete escape sequences, not only ESC (which leaves fake colour text).
    text = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", text)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    return "".join(" " if char == "|" or unicodedata.category(char).startswith("C") else char for char in text)


def DisplayWidth(text: str) -> int:
    return sum(
        0
        if unicodedata.category(char) in {"Mn", "Me"}
        else 2
        if unicodedata.east_asian_width(char) in {"W", "F"}
        else 1
        for char in text
    )


def FormatProgressField(text: str, width: int = 0, *, level: ProgressLevel = ProgressLevel.NORMAL) -> ProgressField:
    value = CleanProgressText(text)
    return ProgressField(value + " " * max(0, width - DisplayWidth(value)), level)


def ProgressNumber(value: int | None, width: int = 6) -> str:
    return ("-" if value is None else str(value)).rjust(width)


def ProgressDuration(seconds: float) -> str:
    hours, remainder = divmod(int(max(0, seconds)), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


__all__ = [
    "ProgressField",
    "ProgressLevel",
    "ProgressMessage",
    "HighestProgressLevel",
    "CleanProgressText",
    "DisplayWidth",
    "FormatProgressField",
    "ProgressNumber",
    "ProgressDuration",
]

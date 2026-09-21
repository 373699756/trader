"""Stable source naming and priority primitives shared by market adapters."""

from __future__ import annotations

from functools import lru_cache

_SOURCE_PRIORITY = {
    "sina": 1,
    "eastmoney": 2,
    "tencent": 3,
    "akshare": 4,
    "tushare": 5,
    "exchange": 6,
}


@lru_cache(maxsize=32)
def source_name(source: str) -> str:
    return source.strip().lower().split("_", 1)[0].split("-", 1)[0]


def source_priority(source: str) -> int:
    return _SOURCE_PRIORITY.get(source_name(source), 0)


__all__ = ["source_name", "source_priority"]

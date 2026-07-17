"""Tencent targeted quote adapter for candidates and displayed TopK rows."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

from trader.domain.models import MarketQuote

SessionFactory = Callable[[], requests.Session]
_DIRECT_PROXIES = {"http": "", "https": "", "all": ""}


class TencentClient:
    def __init__(self, *, timeout_seconds: float, session_factory: SessionFactory = requests.Session) -> None:
        self._timeout_seconds = timeout_seconds
        self._session_factory = session_factory

    def fetch_quotes(self, codes: Sequence[str], now: datetime | None = None) -> tuple[MarketQuote, ...]:
        normalized = tuple(sorted({code for code in codes if len(code) == 6 and code.isdigit()}))
        if not normalized:
            return ()
        received_at = now or datetime.now(timezone.utc)
        with self._session_factory() as session:
            response = session.get(
                "https://qt.gtimg.cn/q=" + ",".join(_symbol(code) for code in normalized),
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"},
                timeout=self._timeout_seconds,
                proxies=_DIRECT_PROXIES,
            )
            response.raise_for_status()
            text = response.content.decode("gb18030", errors="replace")
        quotes = tuple(
            quote
            for payload in re.findall(r'v_[^=]+="([^"]*)";', text)
            if (quote := _parse_payload(payload, received_at, set(normalized))) is not None
        )
        if not quotes:
            raise RuntimeError("tencent returned no usable candidate quotes")
        return quotes


def _parse_payload(payload: str, received_at: datetime, requested: set[str]) -> MarketQuote | None:
    fields = payload.split("~")
    if len(fields) < 50:
        return None
    code = fields[2].strip()
    if code not in requested:
        return None
    source_time = _timestamp(fields[30], received_at)
    transaction = fields[35].split("/") if len(fields) > 35 else []
    amount = _number(transaction[2]) if len(transaction) >= 3 else None
    price = _number(fields[3])
    return MarketQuote(
        code=code,
        name=fields[1].strip(),
        price=price,
        previous_close=_number(fields[4]),
        open_price=_number(fields[5]),
        high=_number(fields[33]),
        low=_number(fields[34]),
        pct_change=_number(fields[32]),
        change_5m=None,
        speed=None,
        volume_ratio=_number(fields[49]),
        turnover_rate=_number(fields[38]),
        amount=amount,
        amplitude=_number(fields[43]),
        market_cap=None,
        industry="",
        source="tencent",
        source_time=source_time,
        received_time=received_at,
        data_version=f"tencent:{int(source_time.timestamp())}",
        is_st="ST" in fields[1].upper() or "退" in fields[1],
        is_suspended=price is None or price <= 0,
    )


def _symbol(code: str) -> str:
    return ("sh" if code.startswith("6") else "sz") + code


def _timestamp(raw: str, fallback: datetime) -> datetime:
    try:
        parsed = datetime.strptime(raw.strip(), "%Y%m%d%H%M%S")
    except ValueError:
        return fallback
    return parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))


def _number(raw: object) -> float | None:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


__all__ = ["TencentClient"]

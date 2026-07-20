"""Tencent targeted quote adapter for candidates and displayed TopK rows."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

from trader.domain.models import MarketQuote
from trader.infrastructure.market_data.normalize import MarketQuoteInput, build_market_quote, normalize_quotes, to_float

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
        quotes = normalize_quotes(
            (
                {str(index): value for index, value in enumerate(payload.split("~"))}
                for payload in re.findall(r'v_[^=]+="([^"]*)";', text)
            ),
            received_at,
            normalizer=lambda row, now: _parse_payload(row, now, set(normalized)),
        )
        if not quotes:
            raise RuntimeError("tencent returned no usable candidate quotes")
        return quotes


def _parse_payload(fields: Mapping[str, object], received_at: datetime, requested: set[str]) -> MarketQuote | None:
    if len(fields) < 50:
        return None
    code = str(fields.get("2") or "").strip()
    if len(fields) < 50:
        return None
    if code not in requested:
        return None
    source_time = _timestamp(str(fields.get("30") or ""), received_at)
    transaction = str(fields.get("35") or "").split("/") if len(fields) > 35 else []
    amount = to_float(transaction[2]) if len(transaction) >= 3 else None
    price = to_float(fields.get("3"))
    return build_market_quote(
        MarketQuoteInput(
            code=code,
            name=str(fields.get("1") or "").strip(),
            price=price,
            previous_close=to_float(fields.get("4")),
            open_price=to_float(fields.get("5")),
            high=to_float(fields.get("33")),
            low=to_float(fields.get("34")),
            pct_change=to_float(fields.get("32")),
            change_5m=None,
            speed=None,
            volume_ratio=to_float(fields.get("49")),
            turnover_rate=to_float(fields.get("38")),
            amount=amount,
            amplitude=to_float(fields.get("43")),
            market_cap=None,
            industry="",
            source="tencent",
            source_time=source_time,
            received_time=received_at,
            data_version=f"tencent:{int(source_time.timestamp())}",
            is_st="ST" in str(fields.get("1") or "").upper() or "退" in str(fields.get("1") or ""),
            is_suspended=price is None or price <= 0,
        )
    )


def _symbol(code: str) -> str:
    return ("sh" if code.startswith("6") else "sz") + code


def _timestamp(raw: str, fallback: datetime) -> datetime:
    try:
        parsed = datetime.strptime(raw.strip(), "%Y%m%d%H%M%S")
    except ValueError:
        return fallback
    return parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))


__all__ = ["TencentClient"]
